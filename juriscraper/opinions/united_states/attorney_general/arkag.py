"""Scraper for the Arkansas Attorney General
CourtID: arkag
Court Short Name: Arkansas Attorney General
"""

import re
from datetime import date
from typing import Any, cast
from urllib.parse import urljoin

from typing_extensions import override

from juriscraper.lib.exceptions import ParsingException
from juriscraper.OpinionSite import ExtractedMetaData
from juriscraper.OpinionSiteLinear import OpinionSiteLinear

# People who sign these opinions. The deputy named in "prepared this
# opinion, which I hereby approve" drafted it; Columbia records the
# signer as the judge. Each row is first name, surname, canonical form.
_SIGNERS = (
    ("DUSTIN", "MCDANIEL", "DUSTIN McDANIEL, Attorney General"),
    ("LESLIE", "RUTLEDGE", "LESLIE RUTLEDGE, Attorney General"),
    ("TIM", "GRIFFIN", "TIM GRIFFIN, Attorney General"),
    # Signed when the Attorney General recused. Seen on 2013-078 and 2026-118.
    (
        "BRADFORD",
        "PHELPS",
        "BRADFORD J. PHELPS, Chief Deputy Attorney General",
    ),
    ("ZACH", "MAYO", "ZACH MAYO, Chief Deputy Attorney General"),
)


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein distance, capped so very different strings stay far."""
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[-1] + 1,
                    previous[j - 1] + (char != other),
                )
            )
        previous = current
    return previous[-1]


def _signer_in(window: str) -> str:
    """Return the canonical signer if the signature window names one."""
    tokens = re.findall(r"[A-Za-z]+", window.upper())
    best = ""
    best_score = 99
    for first, last, canonical in _SIGNERS:
        last_hits = [
            (i, _edit_distance(token, last))
            for i, token in enumerate(tokens)
            if _edit_distance(token, last) <= 2
        ]
        if not last_hits:
            continue
        first_hits = []
        for i, token in enumerate(tokens):
            distance = _edit_distance(token, first)
            # "USTIN" / "STIN" are DUSTIN with the front letters dropped.
            if len(token) >= 4 and token in first:
                distance = min(distance, 1)
            if distance <= 2:
                first_hits.append((i, distance))
        pairs = [
            last_d + first_d
            for i, last_d in last_hits
            for j, first_d in first_hits
            if i != j and (last_d <= 1 or first_d == 0)
        ]
        if not pairs:
            continue
        score = min(pairs)
        if score < best_score:
            best_score = score
            best = canonical
    return best


class Site(OpinionSiteLinear):
    home_url = "https://prod.opinions-search.arkansasag.gov/"
    index_name = "opinions"
    # CourtListener's Columbia import covers opinions through 2011.
    # 1984 is where the Arkansas site's data starts.
    first_opinion_year = 2012
    _bundle_re = re.compile(r'<script[^>]+src="([^"]+\.js)"', re.I)
    # Read from that bundle. A non-meilisearch.io host needs this updated.
    _credentials_re = re.compile(
        r'"(https://[^"]+\.meilisearch\.io)"\s*,\s*'
        r'(?:[A-Za-z_$][\w$]*\s*=\s*)?"([^"\s]{20,})"'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.court_id = self.__module__
        self.url = self.home_url
        self.status = "Published"
        self.should_have_results = True
        # Azure serves these PDFs as application/octet-stream.
        self.expected_content_types = [
            "application/pdf",
            "application/octet-stream",
        ]
        self.results_limit = 10
        self.opinion_filter = None
        self.parameters = {}
        self.make_backscrape_iterable(kwargs)

    @override
    def make_backscrape_iterable(self, kwargs: dict) -> None:
        # Plain years, both ends inclusive: --backscrape-start 2012.
        start = kwargs.get("backscrape_start") or self.first_opinion_year
        end = kwargs.get("backscrape_end") or date.today().year
        self.back_scrape_iterable = list(range(int(start), int(end) + 1))

    def _search_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "q": "",
            "limit": self.results_limit,
            "sort": ["Date_Published:desc"],
            "attributesToRetrieve": [
                "OpenFileNo",
                "Date_Published",
                "RequestorNameFull",
                "OpinionSummary",
                "PDFUrl",
            ],
        }
        if self.opinion_filter:
            payload["filter"] = self.opinion_filter
        return payload

    async def _fetch_text(self, url: str) -> str:
        response = await self.request["session"].get(
            url, headers=self.request["headers"], timeout=60
        )
        response.raise_for_status()
        return response.text

    async def _fetch_credentials(self) -> tuple[str, str]:
        page = await self._fetch_text(self.home_url)
        for src in self._bundle_re.findall(page):
            script = await self._fetch_text(urljoin(self.home_url, src))
            if match := self._credentials_re.search(script):
                return match.group(1), match.group(2)
        raise ParsingException(
            "arkag: could not find the Meilisearch search key"
        )

    @override
    async def _download(self, request_dict=None):
        if self.test_mode_enabled():
            return await super()._download(request_dict)

        host, api_key = await self._fetch_credentials()
        self.url = f"{host}/indexes/{self.index_name}/search"
        self.method = "POST"
        self.request["parameters"]["json"] = self._search_payload()
        self.request["headers"]["Authorization"] = f"Bearer {api_key}"
        self.request["headers"]["Accept"] = "application/json"
        return await super()._download(request_dict)

    @override
    async def _download_backwards(self, year: int) -> None:
        # Both prefixes. The largest year is under 500, so limit 1000.
        self.results_limit = 1000
        self.opinion_filter = (
            f'OpenFileNo STARTS WITH "{year}-" OR '
            f'OpenFileNo STARTS WITH "{year % 100:02d}-"'
        )
        self.html = await self._download()
        self._process_html()

    @staticmethod
    def _summary(hit: dict[str, Any]) -> str:
        requester = hit["RequestorNameFull"].strip()
        sep = "" if requester.endswith(".") else "."
        return f"Requester: {requester}{sep} {hit['OpinionSummary']}"

    @override
    def _process_html(self) -> None:
        response = cast(dict[str, Any], self.html)
        for hit in response["hits"]:
            docket = hit["OpenFileNo"]
            self.cases.append(
                {
                    "docket": docket,
                    "name": f"Arkansas Attorney General Opinion No. {docket}",
                    "date": hit["Date_Published"].split("T", 1)[0],
                    # Date_Published can differ from the date on the letter.
                    "date_filed_is_approximate": True,
                    "url": hit["PDFUrl"].split("?")[0],
                    "summary": self._summary(hit),
                }
            )

    @override
    def extract_from_text(self, scraped_text: str) -> ExtractedMetaData:
        """Signer after the first "Sincerely", if it matches a known name.

        Later closings belong to opinions enclosed with the letter.
        A signature image that OCR cannot read is left blank.
        """
        text = (
            scraped_text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\u00a0", " ")
        )
        closes = list(re.finditer(r"(?m)^[ \t]*sincerely\b", text, re.I))
        if not closes:
            return {}
        tail = text[closes[0].end() : closes[0].end() + 400]
        title = re.search(
            r"(?im)^[ \t]*(?:chief deputy )?attorney general\b",
            tail,
        )
        window = tail[: title.start()] if title else tail[:120]
        signer = _signer_in(window)
        if not signer:
            return {}
        return {"OpinionCluster": {"judges": signer}}
