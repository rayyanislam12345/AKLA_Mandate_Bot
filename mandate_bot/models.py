from dataclasses import dataclass, field


@dataclass
class Tender:
    notice_type: str        # "Tender Notice" / "Corrigendum" / ...
    title: str               # Procurement Name
    category: str            # Type column: Goods / Services / Work / Consultancy ...
    publish_date: str
    close_date: str
    department: str
    status: str
    notice_url: str | None = None     # "Tender Notice" PDF (scanned ad)
    document_url: str | None = None   # "Bidding Document" PDF (full package)
    source: str = "punjab"            # which portal this tender came from

    @property
    def key(self) -> str:
        """Stable dedupe key: prefer the bidding document URL, else the notice URL."""
        return self.document_url or self.notice_url or f"{self.title}|{self.department}|{self.publish_date}"
