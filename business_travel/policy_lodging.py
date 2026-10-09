"""Exact, source-bound lodging limits; no geocoding or staff-rank inference."""

import re
from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from .contracts import Contract


class SeasonWindow(Contract):
    start_month: int = Field(ge=1, le=12)
    start_day: int = Field(ge=1, le=31)
    end_month: int = Field(ge=1, le=12)
    end_day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def valid_days(self):
        date(2000, self.start_month, self.start_day)
        date(2000, self.end_month, self.end_day)
        return self

    def includes(self, day: date) -> bool:
        current = (day.month, day.day)
        start, end = (self.start_month, self.start_day), (self.end_month, self.end_day)
        return start <= current <= end if start <= end else current >= start or current <= end


class LodgingRow(Contract):
    id: str = Field(min_length=1)
    source_id: str
    sheet: str
    cell_range: str
    source_cells: dict[str, str]
    region: str = Field(min_length=1)
    localities: list[str] = Field(min_length=1)
    caps: dict[str, Decimal]
    seasonal_localities: list[str] = Field(default_factory=list)
    season_text: str = ""
    seasons: list[SeasonWindow] = Field(default_factory=list)
    seasonal_caps: dict[str, Decimal] = Field(default_factory=dict)
    unit: str = "person_day"
    currency: str = "CNY"

    @model_validator(mode="after")
    def valid_row(self):
        if self.unit != "person_day" or self.currency != "CNY":
            raise ValueError("Unsupported lodging unit or currency")
        if not self.caps or any(not x.is_finite() or x <= 0 for x in self.caps.values()):
            raise ValueError("Positive lodging caps required")
        if any((self.seasons, self.seasonal_caps, self.seasonal_localities, self.season_text)):
            if not all((self.seasons, self.seasonal_caps, self.seasonal_localities, self.season_text)):
                raise ValueError("Incomplete seasonal lodging rule")
            if set(self.caps) != set(self.seasonal_caps):
                raise ValueError("Seasonal ranks differ from ordinary ranks")
            if any(not x.is_finite() or x <= 0 for x in self.seasonal_caps.values()):
                raise ValueError("Positive seasonal caps required")
        return self


def compact(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def place_key(value: str) -> str:
    # Administrative suffix normalization only, never substring or geographic inference.
    value = compact(value)
    if value in {"全市", "全省", "全区", "其他地区"}:
        return value
    return re.sub(r"(?:[市省]|(?:壮族|回族|维吾尔)?自治区)$", "", value)


def parse_seasons(text: str) -> list[SeasonWindow]:
    import calendar
    windows = []
    for part in compact(text).split("、"):
        match = re.fullmatch(r"(\d{1,2})(?:月)?-(\d{1,2})月(上旬|中旬|下旬)?", part)
        if not match:
            raise ValueError("Unsupported season syntax; review source explicitly")
        start, end = int(match[1]), int(match[2])
        if not 1 <= start <= 12 or not 1 <= end <= 12:
            raise ValueError("Invalid season month")
        end_day = {"上旬": 10, "中旬": 20}.get(match[3], calendar.monthrange(2000, end)[1])
        windows.append(SeasonWindow(start_month=start, start_day=1, end_month=end, end_day=end_day))
    return windows


def lookup_lodging(rows: list[LodgingRow], *, destination: str, province: str | None,
                   standard: str | None, day: date | None) -> dict:
    unknown = {"status": "insufficient_information", "approved": False}
    missing = [key for key, value in (("destination", destination), ("lodging_standard", standard)) if not value]
    if missing:
        return {**unknown, "reason": "lodging_inputs_required", "missing_fields": missing}
    headers = {header for row in rows for header in row.caps}
    if standard not in headers:
        candidates = {header for header in headers
                      if standard in {header + suffix for suffix in ("档", "档次", "标准")}}
        if len(candidates) == 1:
            standard = candidates.pop()
    city, region = place_key(destination), place_key(province or "")
    exact = []
    fallback = []
    for row in rows:
        if region and place_key(row.region) != region and place_key(row.region) != city:
            continue
        local = {place_key(x) for x in row.localities}
        seasonal = {place_key(x) for x in row.seasonal_localities}
        if city in local or city in seasonal:
            exact.append(row)
        elif place_key(row.region) == city and "全市" in local:
            exact.append(row)
        elif region and place_key(row.region) == region and local & {"全省", "全区"}:
            fallback.append(row)
        elif place_key(row.region) == city and local & {"全省", "全区"}:
            fallback.append(row)
    matches = exact or fallback
    if not matches:
        return {**unknown, "reason": "lodging_location_required",
                "missing_fields": ["destination_province", "destination"]}
    # Unknown cities/districts do not establish membership in a residual "other areas" row.
    named = {place_key(x) for row in rows for x in row.localities
             if x not in {"全省", "全区", "全市", "其他地区"}}
    if not exact and city in named:
        return {**unknown, "reason": "lodging_location_required", "missing_fields": ["destination_province"]}
    applicable = []
    for row in matches:
        if standard not in row.caps:
            return {**unknown, "reason": "lodging_standard_required", "missing_fields": ["lodging_standard"]}
        season_applies = (city in {place_key(x) for x in row.seasonal_localities}
                          or bool(set(row.seasonal_localities) & {"全市", "全省", "全区", "其他地区"}))
        if (day is None and season_applies and row.seasons
                and row.seasonal_caps[standard] != row.caps[standard]):
            return {**unknown, "reason": "lodging_date_required",
                    "missing_fields": ["expense_date"], "date": None,
                    "row_ids": list(dict.fromkeys(item.id for item in matches))}
        in_season = bool(day and season_applies and any(window.includes(day) for window in row.seasons))
        applicable.append((row, in_season, (row.seasonal_caps if in_season else row.caps)[standard]))
    seasonal = [item for item in applicable if item[1]]
    chosen = seasonal or applicable
    if len({item[2] for item in chosen}) != 1:
        return {**unknown, "reason": "lodging_region_ambiguous", "missing_fields": ["destination"]}
    return {"status": "ok", "approved": False, "cap": str(chosen[0][2]), "unit": "person_day",
            "currency": chosen[0][0].currency, "seasonal": bool(seasonal),
            "lodging_standard": standard, "date": day.isoformat() if day else None,
            "row_ids": list(dict.fromkeys(item[0].id for item in chosen))}


def rows_from_snapshot(snapshot: dict, layout: dict, source_id: str) -> list[LodgingRow]:
    """Consume an explicit sheet layout; blank region cells resolve only within a numbered group."""
    if snapshot.get("schema_version") != 1 or snapshot.get("sheet") != layout["sheet"]:
        raise ValueError("Sheet snapshot does not match the selected attachment")
    all_rows = {row["row"]: row for row in snapshot["rows"]}

    def cell(row, col):
        return row["cells"][col - 1]

    def value(row, col):
        return compact(cell(row, col)["value"])

    if value(all_rows[layout["unit_row"]], layout["unit_column"]) != layout["unit_label"]:
        raise ValueError("Attachment unit changed")
    header = all_rows[layout["header_row"]]
    grades = [value(header, col) for col in layout["cap_columns"]]
    if grades != layout["grades"]:
        raise ValueError("Attachment grade headers changed")
    selected = [all_rows[number] for number in range(layout["first_row"], layout["last_row"] + 1)]
    groups = {}
    for row in selected:
        group = value(row, layout["group_column"])
        name = value(row, layout["region_column"])
        if name:
            groups.setdefault(group, {})[name] = cell(row, layout["region_column"])["source"]
    result = []
    def column_name(number):
        name = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            name = chr(65 + remainder) + name
        return name

    columns = [layout[key] for key in ("region_column", "locality_column", "season_column",
                                      "season_region_column")]
    columns += layout["cap_columns"] + layout["season_cap_columns"]
    for row in selected:
        group = value(row, layout["group_column"])
        names = groups.get(group, {})
        if len(names) != 1:
            raise ValueError("Missing or conflicting region within numbered group")
        region, region_cell = next(iter(names.items()))
        season_text = value(row, layout["season_column"])
        season_places = value(row, layout["season_region_column"])
        seasonal_values = [value(row, col) for col in layout["season_cap_columns"]]
        if any([season_text, season_places, *seasonal_values]) and not all(
                [season_text, season_places, *seasonal_values]):
            raise ValueError("Partial season cells require review")
        caps = {grade: Decimal(value(row, col)) for grade, col in zip(grades, layout["cap_columns"], strict=True)}
        source_cells = {"region": region_cell}
        for col in (layout["locality_column"], *layout["cap_columns"], layout["season_column"],
                    layout["season_region_column"], *layout["season_cap_columns"]):
            source_cells[str(col)] = cell(row, col)["source"]
        result.append(LodgingRow(
            id=f"lodging-r{row['row']}", source_id=source_id, sheet=snapshot["sheet"],
            cell_range=f"{column_name(min(columns))}{row['row']}:{column_name(max(columns))}{row['row']}",
            source_cells=source_cells,
            region=region, localities=value(row, layout["locality_column"]).split("、"),
            caps=caps, season_text=season_text,
            seasonal_localities=season_places.split("、") if season_places else [],
            seasons=parse_seasons(season_text) if season_text else [],
            seasonal_caps={grade: Decimal(amount) for grade, amount in zip(grades, seasonal_values, strict=True)}
            if season_text else {}))
    return result
