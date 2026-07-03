from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

@dataclass
class CompanyRow:
    agent: str
    country: str = ""
    domain: str = ""
    nickname: str = ""
    period_type: str = "Year End"
    statement_url: str = ""
    landing_url: str = ""
    # Extended fields from full XLS format
    auto_id: str = ""
    client_id: str = ""
    source_url_id: str = ""
    expected_year: str = ""
    mob_id: str = ""
    region: str = ""

@dataclass
class CompanyResult:
    run_id: str
    agent: str
    country: str
    status: str  # "pending" | "running" | "found" | "not_found" | "error"
    pdf_url: str = ""
    s3_key: str = ""
    landing_url: str = ""
    nickname: str = ""
    sub_links: list = field(default_factory=list)
    statements: str = ""
    error_msg: str = ""
    updated_at: Optional[datetime] = None
