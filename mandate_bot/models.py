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
    extra_urls: list[str] = field(default_factory=list)  # corrigenda, addenda, minutes, etc.
    tender_ref: str = ""              # portal's own reference/ID number, when available
    raw_content: str = ""             # full notice text/HTML, when the listing API already includes it (e.g. worldbank)
    # Set by a source whose URLs are not one-per-opportunity, so the URL
    # preference below would pick the wrong identity (ADB: the Project link
    # is a project page that several advertised packages share).
    dedupe_override: str = ""

    @property
    def key(self) -> str:
        """Stable dedupe key: a source's own explicit key if it set one, then
        the bidding document URL, then the notice URL, then the portal's own
        reference number (for sources with no per-tender URL at listing time,
        e.g. Sindh)."""
        return (self.dedupe_override or self.document_url or self.notice_url
                or (f"{self.source}:{self.tender_ref}" if self.tender_ref else None)
                or f"{self.title}|{self.department}|{self.publish_date}")
