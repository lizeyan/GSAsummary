import json
import quopri
import smtplib
import sys
import os
import email
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import lru_cache, reduce
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs

import emlx
import requests
from emlx.emlx import Emlx
from jinja2 import Environment, FileSystemLoader
from loguru import logger
from pyquery import PyQuery
# from scholarly import scholarly

from dotenv import dotenv_values

# ================= Configuration ===============
config = {
    **dotenv_values(".env.shared"),  # load shared development variables
    **dotenv_values(".env.secret"),  # load secret variables
    **os.environ,  # override loaded values with environment variables
}

TARGET_EMAIL = config['GSAS_MAIL_SUMMARY_TARGET_MAIL']
ME = config['GSAS_MAIL_SUMMARY_FROM']
MAIL_PASSWORD = config['GSAS_MAIL_PASSWORD'].rstrip()
MAIL_USER = config['GSAS_MAIL_USER']
LOG_PATH = config['GSAS_LOG_PATH']


data_root = Path(config['GSAS_PATH']).expanduser()
latest_scholar_emails = []

# ==============================================




def is_path_latest(path: Path):
    return path.stat().st_mtime > DATETIME_THRESHOLD.timestamp()


def parse_scholar_url(url: str) -> str:
    try:
        return parse_qs(urlparse(url).query)["url"][0]
    except Exception as e:
        logger.error(f"Parse {url=} failed: {e}")
        return url


def is_scholar_alert(sender):
    return "<scholaralerts-noreply@google.com>" in sender \
           or "<scholarcitations-noreply@google.com>" in sender \
           or "Google Scholar" in sender \
           or "Google学术" in sender


PAPER_ITEM = namedtuple(
    "PAPER_ITEM",
    [
        "title", "abstract", "venue_year", "authors",
        "doi", "type", "url", 'reason', 'date'
    ]
)


@lru_cache(maxsize=None)
def dblp_search(title: str) -> Optional[dict]:
    logger.debug(f"Search {title=} in DBLP")
    try:
        rsp = requests.get("https://dblp.org/search/publ/api", params={"q": title, "format": "json"}).json()
        if rsp["result"]["hits"]["@total"] != '0':
            return rsp["result"]["hits"]["hit"][0]["info"]
        else:
            return None
    except Exception as e:
        logger.error(f"Search from DBLP with {title} failed: {e}")
        return None


@lru_cache(maxsize=None)
def google_scholar_search(title: str) -> Optional[dict]:
    return None # not implemented
    # logger.debug(f"Search {title=} in Google Scholar")
    # try:
    #     rsp = list(scholarly.search_pubs(title))
    #     if len(rsp) > 0:
    #         return rsp[0]
    #     else:
    #         return None
    # except Exception as e:
    #     logger.error(f"Search from Google Scholar with {title} failed: {e}")
    #     return None


@lru_cache(maxsize=None)
def get_paper_detail_from_dblp(
        *, title: str, scholar_abstract: str, scholar_author_venue: str, url: str, reason: str, date,
) -> PAPER_ITEM:
    try:
        scholar_author = scholar_author_venue.split(" - ")[0]
        scholar_venue_year = scholar_author_venue.split(" - ")[1]
    except Exception as e:
        logger.error(f"parse {scholar_author_venue=} failed: {e}")
        scholar_author = scholar_author_venue
        scholar_venue_year = ""
    scholar_url = url
    try:
        d_hit = dblp_search(title=title)
        g_hit = google_scholar_search(title=title)
        if d_hit is not None or g_hit is not None:
            if d_hit is not None and "venue" in d_hit and "year" in d_hit:
                venue_year = f'{d_hit["venue"]}, {d_hit["year"]}'
            elif g_hit is not None and "bib" in g_hit and "venue" in g_hit['bib'] and "pub_year" in g_hit['bib']:
                venue_year = f'{g_hit["bib"]["venue"]}, {g_hit["bib"]["year"]}'
            else:
                venue_year = scholar_venue_year
            if d_hit is not None and "authors" in d_hit:
                authors = ", ".join([_["text"] for _ in d_hit["authors"]["author"]])
            elif g_hit is not None and "bib" in g_hit and "author" in g_hit['bib']:
                authors = ", ".join(g_hit["bib"]["author"])
            else:
                authors = scholar_author

            if d_hit is not None and "doi" in d_hit:
                url = f'https://doi.org/{d_hit["doi"]}'
                doi = d_hit["doi"]
            else:
                url = scholar_url
                doi = ""

            if d_hit is not None and "type" in d_hit:
                pub_type = d_hit["type"]
            else:
                pub_type = ""

            if g_hit is not None and "bib" in g_hit and "abstract" in g_hit["bib"]:
                abstract = g_hit["bib"]["abstract"]
            else:
                abstract = scholar_abstract
            return PAPER_ITEM(
                title=title,
                abstract=abstract,
                venue_year=venue_year,
                authors=authors,
                doi=doi,
                type=pub_type,
                url=url,
                reason=reason,
                date=date
            )
    except Exception as e:
        logger.error(f"Encounter exception when querying {title=}: {e}")
    return PAPER_ITEM(
        title=title,
        abstract=scholar_abstract,
        venue_year=scholar_venue_year,
        doi="",
        type="",
        authors=scholar_author,
        url=scholar_url,
        reason=reason,
        date=date
    )

def extract_from_emlx(email_path: Path):
    email: Emlx = emlx.read(str(email_path), plist_only=True)

    data_received = datetime.fromtimestamp(email.plist["date-received"])
    if not data_received >= DATETIME_THRESHOLD:
        return None

    email: Emlx = emlx.read(str(email_path), plist_only=False)
    if not is_scholar_alert(email.headers["From"]):
        return None

    return {
        'Received': email.headers["Received"],
        'From': email.headers["From"],
        'Subject': email.headers.get('Subject', None),
        'Date': email.headers.get('Date', None),
        'html': getattr(email, "html"),
    }


def extract_from_eml(email_path: Path):
    with open(str(email_path)) as email_file:
        e = email.message_from_file(email_file)

        if not is_scholar_alert(e["From"]):
            return None

        return {
            'Received': e["Received"],
            'From': e["From"],
            'Subject': e["Subject"],
            'Date': e["Date"],
            'html': e.get_payload(),
        }

def parse_raw_mail(subject, date, html):
    pq = PyQuery(quopri.decodestring(html))
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
    __paper_titles = "\n\t\t\t\t".join(titles)
    reason = pq('p')[-1].text_content().encode('latin1').decode('utf-8')

    date = email.utils.parsedate_to_datetime(date).strftime('%Y-%m-%d')
    logger.info(
        f"Parsed Email Detail: \n"
        f"\tSubject:\t{subject}\n"
        f"\tDate:\t\t{date}\n"
        f"\tReason:\t\t{reason}\n"
        f"\tPapers:\t\t{__paper_titles}\n"
    )
    return titles, abstracts, author_venues, urls, reason, date




def parse_email_from_path(email_path: Path) -> Dict[str, PAPER_ITEM]:
    try:
        if email_path.suffix == '.emlx':
            mail_content = extract_from_emlx(email_path)
        else:
            mail_content = extract_from_eml(email_path)

        if mail_content is None:
            return {}

        ret = {}
        titles, abstracts, author_venues, urls, reason, date = parse_raw_mail(mail_content['Subject'], mail_content['Date'], mail_content['html'])
        for title, abstract, author_venue, url in zip(titles, abstracts, author_venues, urls):
            ret[title] = get_paper_detail_from_dblp(
                title=title, scholar_abstract=abstract, scholar_author_venue=author_venue, url=url,
                reason=reason, date=date
            )
        return ret
    except Exception as e:
        logger.exception(f"Encounter exception when processing {email_path=}: {e}")
        return {}


def group_papers_by_end_date(papers):
    return [
            {
                'title': END_DATE_STR,
                'papers': list(sorted(papers.values(), key=lambda _: _["title"].lower()))
             }
    ]

def group_papers_by_date(papers):

    by_date = list(sorted(papers.values(), key=lambda _: (_["date"].lower(), _["title"].lower())))

    last_date = ''

    groups = []

    for p in by_date:
        if p['date'] != last_date:
            groups.append({
                'title': p['date'],
                'papers': []
            })
            last_date = p['date']
        groups[-1]['papers'].append(p)

    return groups

def render_report():
    env = Environment(loader=FileSystemLoader("./templates"))
    with open(f"output/{END_DATE_STR}.json", "r") as f:
        papers = json.load(f)
    template = env.get_template("report.html")
    with open(f"output/{END_DATE_STR}.html", "w+") as f:

        groups = group_papers_by_date(papers)

        print(template.render(
            groups=groups,
            start_datetime=DATETIME_THRESHOLD.strftime("%Y-%m-%d"),
            end_datetime=END_DATE_STR
        ), file=f)


def send_email():
    if "" in [TARGET_EMAIL, MAIL_USER]:
        return

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
        server.login(MAIL_USER, MAIL_PASSWORD)
        server.send_message(msg)


def parse_papers_from_emails():
    with ThreadPoolExecutor() as executor:
        paper_dicts_of_emails = list(executor.map(
            parse_email_from_path,
            filter(is_path_latest, data_root.glob("**/*.eml*"))
        ))
    if len(paper_dicts_of_emails) == 0:
        logger.info("No recent alert found.")
        return
    papers = {}
    for paper_dict in paper_dicts_of_emails:
        for title, paper in paper_dict.items():
            if title not in papers:
                reason = [paper.reason]
            else:
                reason = papers[title]["reason"] + [paper.reason]
            papers[title] = paper._asdict()
            papers[title]["reason"] = reason
    with open(f"output/{END_DATE_STR}.json", "w+") as f:
        json.dump(papers, f, indent=4, ensure_ascii=False)
    return len(papers)


def main():
    res = parse_papers_from_emails()

    if res is None:
        logger.info("No alerts found.")
        return

    if res == 0:
        logger.info("No recent paper found.")
        return

    render_report()
    send_email()


def set_proxy():
    try:
        pg = ProxyGenerator()
        pg.FreeProxies()
        scholarly.use_proxy(pg)
    except Exception as e:
        logger.error(f"Use proxy error: {e}")


if __name__ == '__main__':
    try:
        logger.add(
            os.path.expanduser(LOG_PATH), rotation="1 week", retention="1 month",
            enqueue=True, encoding="utf-8",
            compression="zip", level="INFO",
        )

        # Create output directory
        Path("output").mkdir(parents=True, exist_ok=True)

        # Parse arguments
        num_days = int(sys.argv[1]) if len(sys.argv) > 1 else 1

        if len(sys.argv) > 2:
            data_root = Path(sys.argv[2]).expanduser()

        END_DATE_STR = datetime.now().strftime('%Y-%m-%d')

        if num_days > 0:
            DATETIME_THRESHOLD = datetime.now() - timedelta(days=num_days)
        else:
            DATETIME_THRESHOLD = datetime.now() - timedelta(weeks=1000*12*4)

        logger.info(f"START {DATETIME_THRESHOLD} - {END_DATE_STR}")
        # set_proxy()
        main()
    except Exception as e:
        logger.exception("GSASummary failed", exception=e)
