import json
import quopri
import smtplib
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import lru_cache, reduce
from pathlib import Path
from typing import Dict
from urllib.parse import urlparse, parse_qs

import emlx
import requests
from emlx.emlx import Emlx
from jinja2 import Environment, FileSystemLoader
from loguru import logger
from pyquery import PyQuery

data_root = Path("~/Library/Mail").expanduser()
latest_scholar_emails = []

# ================= Change These ===============
TARGET_EMAIL = "li_zeyan@icloud.com"
ME = "Zeyan Li <li_zeyan@icloud.com>"
# ==============================================


END_DATE_STR = datetime.now().strftime('%Y-%m-%d')
DATETIME_THRESHOLD = datetime.now() - timedelta(days=1)


def is_email_content_latest(email: Emlx):
    data_received = datetime.fromtimestamp(email.plist["date-received"])
    return data_received >= DATETIME_THRESHOLD


def is_path_latest(path: Path):
    return path.stat(follow_symlinks=True).st_mtime > DATETIME_THRESHOLD.timestamp()


def parse_scholar_url(url: str) -> str:
    try:
        return parse_qs(urlparse(url).query)["url"][0]
    except Exception as e:
        logger.error(f"Parse {url=} failed: {e}")
        return url


def is_scholar_alert(email: Emlx):
    sender = email.headers["From"]
    return "<scholaralerts-noreply@google.com>" in sender \
           or "<scholarcitations-noreply@google.com>" in sender \
           or "Google Scholar" in sender \
           or "Google学术" in sender


PAPER_ITEM = namedtuple(
    "PAPER_ITEM",
    [
        "title", "abstract", "venue_year", "authors",
        "doi", "type", "url",
    ]
)


@lru_cache(maxsize=None)
def get_paper_detail_from_dblp(*, title: str, abstract: str, author_venue: str, url: str) -> PAPER_ITEM:
    try:
        scholar_author = author_venue.split(" - ")[0]
        scholar_venue_year = author_venue.split(" - ")[1]
    except Exception as e:
        logger.error(f"parse {author_venue=} failed: {e}")
        scholar_author = author_venue
        scholar_venue_year = ""
    scholar_url = url
    try:
        rsp = requests.get("https://dblp.org/search/publ/api", params={"q": title, "format": "json"}).json()
        if rsp["result"]["hits"]["@total"] != '0':  # if we can find a item in DBLP
            info = rsp["result"]["hits"]["hit"][0]["info"]
            if "venue" in info and "year" in info:
                venue_year = f'{info["venue"]}, {info["year"]}'
            else:
                venue_year = scholar_venue_year
            if "authors" in info:
                authors = ", ".join([_["text"] for _ in info["authors"]["author"]])
            else:
                authors = scholar_author
            if "doi" in info:
                url = f'https://doi.org/{info["doi"]}'
            else:
                url = scholar_url
            return PAPER_ITEM(
                title=title,
                abstract=abstract,
                venue_year=venue_year,
                authors=authors,
                doi=info.get("doi", ""),
                type=info.get("type", ""),
                url=url
            )
        else:
            pass
    except Exception as e:
        logger.error(f"Encounter exception when querying {title=}: {e}")
    return PAPER_ITEM(
        title=title,
        abstract=abstract,
        venue_year=scholar_venue_year,
        doi="",
        type="",
        authors=scholar_author,
        url=scholar_url
    )


def parse_email_from_path(email_path: Path) -> Dict[str, PAPER_ITEM]:
    try:
        email: Emlx = emlx.read(str(email_path), plist_only=True)
        if not is_email_content_latest(email):
            return {}
        email: Emlx = emlx.read(str(email_path), plist_only=False)
        if not is_scholar_alert(email):
            return {}
        pq = PyQuery(quopri.decodestring(getattr(email, "html")))
        titles = [_.text_content().encode("latin1").decode("utf-8") for _ in pq(".gse_alrt_title")]
        abstracts = [
            " ".join(_.text_content().replace("\n", " ").encode("latin1").decode("utf-8").split())
            for _ in pq(".gse_alrt_sni")
        ]
        author_venues = [
            " ".join(_.text_content().replace("\n", " ").encode("latin1").decode("utf-8").split())
            for _ in pq(".gse_alrt_sni").prev()
        ]
        urls = [
            parse_scholar_url(_.attrib["href"])
            for _ in pq(".gse_alrt_title")
        ]
        ret = {}
        for title, abstract, author_venue, url in zip(titles, abstracts, author_venues, urls):
            ret[title] = get_paper_detail_from_dblp(
                title=title, abstract=abstract, author_venue=author_venue, url=url
            )
        return ret
    except Exception as e:
        logger.exception(f"Encounter exception when processing {email_path=}: {e}")
        return {}


def render_report():
    env = Environment(loader=FileSystemLoader("./templates"))
    with open(f"output/{END_DATE_STR}.json", "r") as f:
        papers = json.load(f)
    template = env.get_template("report.html")
    with open(f"output/{END_DATE_STR}.html", "w+") as f:
        print(template.render(
            papers=list(sorted(papers.values(), key=lambda _: _["title"].lower())),
            start_datetime=DATETIME_THRESHOLD.strftime("%Y-%m-%d"),
            end_datetime=END_DATE_STR
        ), file=f)


def send_email():
    msg = EmailMessage()
    msg['Subject'] = f'Google Scholar Alerts Summary on {END_DATE_STR}'
    msg["From"] = ME
    msg["To"] = TARGET_EMAIL
    with open(f"output/{END_DATE_STR}.html", "r") as f:
        msg.set_content(f.read(), subtype="html")
    with smtplib.SMTP('smtp.mail.me.com', 587) as server:
        # identify ourselves
        server.ehlo()
        # secure our email with tls encryption
        server.starttls()
        # re-identify ourselves as an encrypted connection
        server.ehlo()
        with open("MAIL_PASSWORD", 'r') as f:
            server.login('li_zeyan@icloud.com', f.read())
        server.send_message(msg)


def main():
    with ThreadPoolExecutor() as executor:
        papers = list(executor.map(
            parse_email_from_path,
            filter(is_path_latest, data_root.glob("**/*.emlx"))
        ))
    papers = reduce(lambda a, b: a | b, papers)
    papers = {k: v._asdict() for k, v in papers.items()}
    with open(f"output/{END_DATE_STR}.json", "w+") as f:
        json.dump(papers, f, indent=4, ensure_ascii=False)

    render_report()
    send_email()


if __name__ == '__main__':
    logger.add(
        "/usr/local/var/log/GSASummary.log", rotation="1 week", retention="1 month",
        enqueue=True, encoding="utf-8",
        compression="zip", level="INFO",
    )
    main()
