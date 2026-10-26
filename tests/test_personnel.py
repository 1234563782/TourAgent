
"""Account identity overrides chat claims and never crosses the two identities."""

import unittest

from business_travel.personnel import PersonnelProfile, apply_account_identity, stamp_slots


class PersonnelIdentityTests(unittest.TestCase):
    def test_account_identity_is_used_without_a_chat_claim(self):
        profile = PersonnelProfile(academic_title="院士", lodging_standard="其他人员")
        values, notes = apply_account_identity({"destination": "北京"}, profile)
        self.assertEqual(values["academic_title"], "院士")
        self.assertEqual(values["lodging_standard"], "其他人员")
        self.assertEqual(notes, [])

    def test_chat_cannot_raise_either_identity(self):
        profile = PersonnelProfile(academic_title="正高级", technical_grade=5, lodging_standard="其他人员")
        values, notes = apply_account_identity(
            {"academic_title": "院士", "lodging_standard": "部级", "destination": "北京"}, profile)
        self.assertEqual(values["academic_title"], "正高级")
        self.assertEqual(values["technical_grade"], 5)
        self.assertEqual(values["lodging_standard"], "其他人员")
        self.assertEqual(values["destination"], "北京")
        self.assertIn("交通身份", "".join(notes))
        self.assertIn("住宿身份", "".join(notes))

    def test_missing_identity_is_not_filled_from_chat_or_the_other_identity(self):
        profile = PersonnelProfile(academic_title="院士")
        values, notes = apply_account_identity(
            {"academic_title": "院士", "lodging_standard": "部级"}, profile)
        self.assertEqual(values["academic_title"], "院士")
        self.assertNotIn("lodging_standard", values)
        self.assertIn("住宿身份尚未由管理员维护", "".join(notes))
        self.assertNotIn("部级", values.values())

    def test_unmaintained_transport_identity_is_not_taken_from_chat(self):
        values, notes = apply_account_identity(
            {"academic_title": "院士", "technical_grade": 1}, PersonnelProfile())
        self.assertNotIn("academic_title", values)
        self.assertNotIn("technical_grade", values)
        self.assertIn("交通身份尚未由管理员维护", "".join(notes))

    def test_student_activity_remains_a_trip_fact(self):
        profile = PersonnelProfile(personnel_type="student", lodging_standard="其他人员")
        values, notes = apply_account_identity(
            {"personnel_type": "staff", "student_activity_eligible": True}, profile)
        self.assertEqual(values["personnel_type"], "student")
        self.assertIs(values["student_activity_eligible"], True)
        self.assertTrue(notes)

    def test_absent_profile_keeps_existing_callers_unchanged(self):
        values, notes = apply_account_identity({"academic_title": "院士"}, None)
        self.assertEqual(values, {"academic_title": "院士"})
        self.assertEqual(notes, [])

    def test_stamp_slots_updates_subjects_without_cross_filling(self):
        slots, notes = stamp_slots({
            "subjects": {"s1": {"values": {"academic_title": "院士", "lodging_standard": "部级"}}},
            "expense": {"values": {"lodging_standard": "部级"}},
        }, PersonnelProfile(academic_title="正高级"))
        self.assertEqual(slots["subjects"]["s1"]["values"]["academic_title"], "正高级")
        self.assertNotIn("lodging_standard", slots["subjects"]["s1"]["values"])
        self.assertNotIn("lodging_standard", slots["expense"]["values"])
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
