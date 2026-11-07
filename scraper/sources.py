"""Public Nova Scotia Archives virtual exhibits used as sample sources.

These pages are published for public reading. Each item page carries a title,
a short description, and labeled fields such as Date, Photographer, Format,
and Reference. Page-level Dublin Core tags describe the website, not the item,
so item values are read from the record body.
"""

from dataclasses import dataclass


BASE_URL = "https://archives.novascotia.ca"

USER_AGENT = (
    "BCCNS-Hackathon-MetadataScraper/0.1 "
    "(educational hackathon; harvests public virtual-exhibit metadata)"
)


@dataclass(frozen=True)
class Source:
    slug: str
    collection: str
    subjects: tuple[str, ...]
    relation_path: str
    results_path: str

    def results_url(self, start: int) -> str:
        return BASE_URL + self.results_path.format(start=start)

    def item_url(self, item_id: int) -> str:
        return f"{BASE_URL}/{self.slug}/archives/?ID={item_id}"

    @property
    def relation_url(self) -> str:
        return BASE_URL + self.relation_path


SOURCES = (
    Source(
        slug="africanns",
        collection="African Nova Scotians in the Age of Slavery and Abolition",
        subjects=(
            "African Nova Scotians",
            "Slavery",
            "Abolition",
            "Black Loyalists",
            "Black Refugees",
        ),
        relation_path="/africanns/",
        results_path="/africanns/results/?Search=&SearchList1=all&Start={start}",
    ),
    Source(
        slug="diaspora",
        collection="African Nova Scotian Diaspora",
        subjects=(
            "African Nova Scotian Diaspora",
            "Black Refugees",
            "Sierra Leone",
            "Immigration",
        ),
        relation_path="/diaspora/",
        results_path="/diaspora/results/?Search=&Start={start}",
    ),
    Source(
        slug="africville",
        collection="Africville: Gone but Never Forgotten",
        subjects=("Africville", "African Nova Scotians", "Halifax", "Photographs"),
        relation_path="/africville/",
        results_path="/africville/results/?Search=&Start={start}",
    ),
    Source(
        slug="desmond",
        collection="Viola Desmond",
        subjects=("Viola Desmond", "Civil rights", "New Glasgow", "African Nova Scotians"),
        relation_path="/desmond/",
        results_path="/desmond/results/?Search=&Start={start}",
    ),
)
