"""Migration safety and real PostgreSQL roundtrips in disposable schemas."""

import os
import unittest
from dataclasses import replace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from typing import TypedDict

from business_travel.settings import Settings
from business_travel.memory_pg import MemoryIdentity, ScopedPostgresSaver
from business_travel.single_school_migration import (
    migrate_schema, user_thread, without_namespace, SchoolMigrationError,
)


class MigrationUnitTests(unittest.TestCase):
    def test_thread_mapping_is_strict_and_preserves_user_session(self):
        self.assertEqual(user_thread("old_school:alice:web"),("old_school","alice:web"))
        for value in ("alice:web","a:b:c:d","school:../alice:web","school:alice:"):
            with self.assertRaises(SchoolMigrationError): user_thread(value)

    def test_constraint_rewrite_preserves_references_and_delete_semantics(self):
        self.assertEqual(without_namespace(
            'FOREIGN KEY (company_id, user_id) REFERENCES auth_users(company_id, user_id) ON DELETE CASCADE'),
            'FOREIGN KEY (user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE')
        self.assertEqual(without_namespace('CREATE INDEX x ON trips USING btree (company_id, user_id, sequence DESC)'),
                         'CREATE INDEX x ON trips USING btree (user_id, sequence DESC)')


class FlowState(TypedDict):
    value: str
    confirmed: bool


@unittest.skipUnless(os.environ.get("TRAVEL_TEST_POSTGRES")=="1","PostgreSQL integration is opt-in")
class MigrationLiveTests(unittest.TestCase):
    def setUp(self):
        self.schema="school_migration_test_"+uuid4().hex
        self.settings=Settings.from_env()
        self.conn=psycopg.connect(self.settings.memory_database_url,autocommit=True,row_factory=dict_row)
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup)
        self.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        self.conn.execute("""
            CREATE TABLE auth_users(company_id text NOT NULL,user_id text NOT NULL,username text NOT NULL,
                password_hash text NOT NULL,PRIMARY KEY(company_id,user_id),UNIQUE(company_id,username));
            CREATE TABLE auth_sessions(company_id text NOT NULL,user_id text NOT NULL,token_hash text PRIMARY KEY,
                FOREIGN KEY(company_id,user_id) REFERENCES auth_users(company_id,user_id) ON DELETE CASCADE);
            CREATE TABLE conversations(company_id text NOT NULL,user_id text NOT NULL,id text NOT NULL,
                title text,PRIMARY KEY(company_id,user_id,id));
            CREATE TABLE chat_runs(company_id text NOT NULL,user_id text NOT NULL,conversation_id text NOT NULL,
                id text NOT NULL,status text NOT NULL,PRIMARY KEY(company_id,user_id,conversation_id,id),
                FOREIGN KEY(company_id,user_id,conversation_id) REFERENCES conversations(company_id,user_id,id));
            CREATE UNIQUE INDEX chat_runs_one_running ON chat_runs(company_id,user_id,conversation_id) WHERE status='running';
            CREATE TABLE trips(company_id text NOT NULL,user_id text NOT NULL,draft_id text NOT NULL,data jsonb NOT NULL,
                PRIMARY KEY(company_id,user_id,draft_id));
            CREATE TABLE attachments(company_id text NOT NULL,user_id text NOT NULL,attachment_id text NOT NULL,
                original bytea NOT NULL,PRIMARY KEY(company_id,user_id,attachment_id));
            CREATE TABLE attachment_links(company_id text NOT NULL,user_id text NOT NULL,conversation_id text NOT NULL,
                request_id text NOT NULL,attachment_id text NOT NULL,
                PRIMARY KEY(company_id,user_id,conversation_id,request_id,attachment_id),
                FOREIGN KEY(company_id,user_id,attachment_id) REFERENCES attachments(company_id,user_id,attachment_id),
                FOREIGN KEY(company_id,user_id,conversation_id,request_id) REFERENCES chat_runs(company_id,user_id,conversation_id,id));
            CREATE TABLE resource_daily_usage(company_id text NOT NULL,scope text NOT NULL CHECK(scope IN ('company','user')),
                subject_id text NOT NULL,usage_day date NOT NULL,requests bigint NOT NULL,model_calls bigint NOT NULL,
                PRIMARY KEY(company_id,scope,subject_id,usage_day));
            INSERT INTO auth_users VALUES('old','alice','alice','hash');
            INSERT INTO auth_sessions VALUES('old','alice','token_hash');
            INSERT INTO conversations VALUES('old','alice','web','title');
            INSERT INTO chat_runs VALUES('old','alice','web','run1','completed');
            INSERT INTO trips VALUES('old','alice','trip1','{"id":"trip1"}');
            INSERT INTO attachments VALUES('old','alice','file1',decode('0001ff','hex'));
            INSERT INTO attachment_links VALUES('old','alice','web','run1','file1');
            INSERT INTO resource_daily_usage VALUES('old','company','old','2026-10-29',3,7);
            INSERT INTO resource_daily_usage VALUES('old','user','alice','2026-10-29',3,7);
        """)

    def cleanup(self):
        if not self.schema.startswith('school_migration_test_'): raise AssertionError('unsafe cleanup')
        self.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        self.conn.close()

    def columns(self):
        return self.conn.execute("SELECT table_name FROM information_schema.columns WHERE table_schema=%s AND column_name='company_id'",(self.schema,)).fetchall()

    def test_roundtrip_keeps_pending_interrupt_blobs_fk_and_school_quota(self):
        saver=PostgresSaver(self.conn)
        saver.setup()
        def confirm(state): return {"confirmed":interrupt("approve?")}
        builder=StateGraph(FlowState)
        builder.add_node("confirm",confirm)
        builder.add_edge(START,"confirm"); builder.add_edge("confirm",END)
        graph=builder.compile(checkpointer=saver)
        old={"configurable":{"thread_id":"old:alice:web"}}
        graph.invoke({"value":"private", "confirmed":False},old)
        pending=graph.get_state(old)
        self.assertTrue(pending.interrupts)
        before=saver.get_tuple(pending.config)
        dry=migrate_schema(self.conn,self.schema)
        self.assertEqual(dry['status'],'ready')
        self.assertTrue(self.columns())
        report=migrate_schema(self.conn,self.schema,apply=True)
        self.assertEqual(report['status'],'migrated')
        self.assertFalse(self.columns())
        new={"configurable":{"thread_id":"alice:web"}}
        scoped=ScopedPostgresSaver(self.conn,MemoryIdentity('alice'))
        after=scoped.get_tuple({"configurable":{**pending.config['configurable'],'thread_id':'alice:web'}})
        self.assertEqual(before.checkpoint,after.checkpoint)
        self.assertEqual(before.pending_writes,after.pending_writes)
        restored=builder.compile(checkpointer=scoped)
        self.assertEqual(restored.get_state(new).values,pending.values)
        self.assertTrue(restored.invoke(Command(resume=True),new)['confirmed'])
        with self.assertRaises(PermissionError):ScopedPostgresSaver(self.conn,MemoryIdentity('bob')).get_tuple(new)
        self.assertEqual(bytes(self.conn.execute('SELECT original FROM attachments').fetchone()['original']),bytes([0,1,255]))
        self.assertEqual(self.conn.execute("SELECT requests,model_calls FROM resource_daily_usage WHERE scope='school' AND subject_id='school'").fetchone(),{'requests':3,'model_calls':7})
        self.conn.execute("DELETE FROM auth_users WHERE user_id='alice'")
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM auth_sessions').fetchone()['n'],0)
        self.assertEqual(migrate_schema(self.conn,self.schema,apply=True)['status'],'already_migrated')

    def test_multiple_namespaces_abort_without_modifying_records(self):
        self.conn.execute("INSERT INTO auth_users VALUES('other','alice','alice','hash')")
        with self.assertRaisesRegex(SchoolMigrationError,'multiple_legacy'):
            migrate_schema(self.conn,self.schema,apply=True)
        self.assertTrue(self.columns())
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM auth_users').fetchone()['n'],2)

    def test_running_request_blocks_migration(self):
        self.conn.execute("UPDATE chat_runs SET status='running'")
        with self.assertRaisesRegex(SchoolMigrationError,'running_requests'):
            migrate_schema(self.conn,self.schema,apply=True)
        self.assertTrue(self.columns())

    def test_schema_failure_rolls_back_all_changes(self):
        self.conn.execute("""
            CREATE FUNCTION reject_quota_update() RETURNS trigger LANGUAGE plpgsql AS
            $$ BEGIN RAISE EXCEPTION 'synthetic migration failure'; END $$;
            CREATE TRIGGER reject_quota_update BEFORE UPDATE ON resource_daily_usage
            FOR EACH ROW EXECUTE FUNCTION reject_quota_update();
        """)
        with self.assertRaises(psycopg.Error):
            migrate_schema(self.conn,self.schema,apply=True)
        self.assertTrue(self.columns())
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM auth_sessions").fetchone()['n'],1)

    def test_policy_digest_and_keys_migrate_transactionally(self):
        from scripts.convert_school_bundle import convert
        from tests.test_policy_rag import bundle
        import hashlib
        legacy=bundle().model_dump(mode='json')
        old={"schema_version":legacy.pop('schema_version'),"company_id":"old",**legacy}
        converted,mapping=convert(old)
        canonical=converted.model_dump_json(exclude={'sources','lodging_rows','adoption_note'})
        import json
        fields=json.loads(canonical)
        old_canonical={'schema_version':fields.pop('schema_version'),'company_id':'old',**fields}
        self.assertEqual(mapping['old_digest'],hashlib.sha256(json.dumps(old_canonical,ensure_ascii=False,separators=(',',':')).encode()).hexdigest())
        # Use a separate schema to keep policy and memory inventories independent.
        policy_schema=self.schema+'_p'
        self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(policy_schema)))
        try:
            self.conn.execute(sql.SQL('CREATE TABLE {} (id bigint PRIMARY KEY, company_id text NOT NULL,policy_key text NOT NULL,version text NOT NULL,bundle_sha256 text NOT NULL,UNIQUE(company_id,policy_key,version))').format(sql.Identifier(policy_schema,'policy_documents')))
            self.conn.execute(sql.SQL('INSERT INTO {} VALUES(1,%s,%s,%s,%s)').format(sql.Identifier(policy_schema,'policy_documents')),('old',mapping['policy_key'],mapping['version'],mapping['old_digest']))
            with self.assertRaisesRegex(SchoolMigrationError,'policy_digest_mapping_required'):
                migrate_schema(self.conn,policy_schema,kind='policy',apply=True)
            report=migrate_schema(self.conn,policy_schema,kind='policy',apply=True,digest_mappings=[mapping])
            self.assertEqual(report['status'],'migrated')
            row=self.conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(policy_schema,'policy_documents'))).fetchone()
            self.assertNotIn('company_id',row)
            self.assertEqual(row['bundle_sha256'],mapping['new_digest'])
        finally:
            self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(policy_schema)))
