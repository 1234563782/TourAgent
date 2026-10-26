"""Account-bound transport and lodging identities. Chat cannot assign either."""

from pydantic import Field

from .contracts import Contract

TRANSPORT_FIELDS = ("personnel_type", "academic_title", "technical_grade", "management_grade")
LODGING_FIELDS = ("lodging_standard",)
IDENTITY_FIELDS = (*TRANSPORT_FIELDS, *LODGING_FIELDS)
FIELD_LABELS = {
    "personnel_type": "人员类型",
    "academic_title": "职称",
    "technical_grade": "专业技术岗位等级",
    "management_grade": "管理岗位等级",
    "lodging_standard": "住宿人员档次",
}


class PersonnelProfile(Contract):
    personnel_type: str | None = Field(default=None, pattern=r"^(staff|student|invited)$")
    academic_title: str | None = Field(default=None, max_length=40)
    technical_grade: int | None = Field(default=None, ge=1)
    management_grade: int | None = Field(default=None, ge=1)
    lodging_standard: str | None = Field(default=None, pattern=r"^(部级|司局级|其他人员)$")

    def transport_maintained(self):
        return any(getattr(self, field) not in (None, "") for field in TRANSPORT_FIELDS)

    def lodging_maintained(self):
        return self.lodging_standard not in (None, "")


def apply_account_identity(values, profile):
    """Return facts using the account record. A missing identity is not filled from chat."""
    if profile is None:
        return dict(values), []
    updated = dict(values)
    notes = []

    def bind(fields, maintained, missing_note, conflict_note):
        stated = [field for field in fields if updated.get(field) not in (None, "")]
        if not maintained:
            if stated:
                notes.append(missing_note)
            for field in fields:
                updated.pop(field, None)
            return
        conflict = False
        for field in fields:
            account = getattr(profile, field)
            stated_value = updated.get(field)
            if account not in (None, ""):
                if stated_value not in (None, "") and stated_value != account:
                    conflict = True
                updated[field] = account
            elif stated_value not in (None, ""):
                conflict = True
                updated.pop(field, None)
        if conflict:
            notes.append(conflict_note)

    bind(TRANSPORT_FIELDS, profile.transport_maintained(),
         "交通身份尚未由管理员维护，对话不能自行填写。",
         "对话中的交通身份与账号记录不一致，已按账号记录。")
    bind(LODGING_FIELDS, profile.lodging_maintained(),
         "住宿身份尚未由管理员维护，对话不能自行填写，也不能由交通身份推导。",
         "对话中的住宿身份与账号记录不一致，已按账号记录。")
    return updated, notes


def stamp_slots(slots, profile):
    """Apply the account identity after slot review, without treating chat text as maintenance."""
    if profile is None or not isinstance(slots, dict):
        return slots, []
    updated = {**slots}
    notes = []
    subjects = dict(updated.get("subjects") or {})
    for ident, subject in subjects.items():
        if not isinstance(subject, dict) or not isinstance(subject.get("values"), dict):
            continue
        values, item_notes = apply_account_identity(subject["values"], profile)
        stamped = {**subject, "values": values}
        if item_notes:
            stamped["identity_notes"] = item_notes
            notes.extend(item_notes)
        subjects[ident] = stamped
    if subjects:
        updated["subjects"] = subjects
    for domain in ("trip", "expense"):
        subject = updated.get(domain)
        if isinstance(subject, dict) and isinstance(subject.get("values"), dict):
            values, item_notes = apply_account_identity(subject["values"], profile)
            stamped = {**subject, "values": values}
            if item_notes:
                stamped["identity_notes"] = item_notes
                notes.extend(item_notes)
            updated[domain] = stamped
    return updated, list(dict.fromkeys(notes))
