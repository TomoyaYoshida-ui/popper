"""Resolve publicly accessible publisher/DOI PDF links without bypassing access controls."""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .core import ProtocolError, file_hash


def public_https(url):
    parsed = urlparse(str(url or ""))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".localhost")):
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_global
    except ValueError:
        return "." in parsed.hostname


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_https(newurl):
            raise ProtocolError("全文链接重定向到非公开 HTTPS 地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit=50_000_000):
    if not public_https(url):
        raise ProtocolError("全文地址必须是公开 HTTPS 地址")
    request = urllib.request.Request(url, headers={"User-Agent": "Popper/0.1 research verification",
                                                  "Accept": "application/pdf,text/html;q=0.9,application/json;q=0.8,*/*;q=0.5"})
    with urllib.request.build_opener(PublicRedirect()).open(request, timeout=15) as response:
        data = response.read(limit + 1)
        final_url = response.geturl()
    if len(data) > limit:
        raise ProtocolError("全文响应超过大小上限")
    return final_url, data


class PdfLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        link = None
        if tag == "meta" and attrs.get("name", "").lower() in {"citation_pdf_url", "wkhealth_pdf_url"}:
            link = attrs.get("content")
        elif tag == "a" and attrs.get("type", "").lower() == "application/pdf":
            link = attrs.get("href")
        if link:
            resolved = urljoin(self.base, link)
            if public_https(resolved) and resolved not in self.links:
                self.links.append(resolved)


def pdf_candidates(paper):
    urls = []
    for key in ("pdf_url", "open_access_pdf", "openAccessPdf", "best_oa_location", "primary_location"):
        value = paper.get(key)
        if isinstance(value, dict):
            value = value.get("url_for_pdf") or value.get("pdf_url") or value.get("url")
        if value and public_https(value):
            urls.append(str(value))
    url = str(paper.get("url") or "")
    arxiv = paper.get("arxiv_id")
    if "arxiv.org/abs/" in url:
        arxiv = url.split("/abs/", 1)[1].split("?", 1)[0]
    if arxiv and re.fullmatch(r"[A-Za-z0-9./-]+", str(arxiv)):
        urls.append("https://arxiv.org/pdf/" + str(arxiv))
    if public_https(url) and urlparse(url).path.lower().endswith(".pdf"):
        urls.append(url)
    doi = str(paper.get("doi") or "").removeprefix("https://doi.org/").strip()
    if doi.startswith("10.") and "/" in doi:
        urls.append("https://doi.org/" + quote(doi, safe="/().-"))
    if public_https(url):
        urls.append(url)
    return list(dict.fromkeys(urls))


def extract_pdf(data, output_dir, paper_id, source_url):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(paper_id)):
        raise ProtocolError("论文产物标识不合法")
    if not data.startswith(b"%PDF-") or len(data) > 50_000_000:
        raise ProtocolError("下载内容不是有效的受限大小 PDF")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf, text = output_dir / f"{paper_id}.pdf", output_dir / f"{paper_id}.txt"
    pdf.write_bytes(data)
    executable = shutil.which("pdftotext")
    if executable:
        completed = subprocess.run([executable, "-layout", str(pdf), str(text)],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise ProtocolError("pdftotext 全文提取失败")
        engine = "pdftotext"
    else:
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ProtocolError("缺少 PDF 提取器：安装 research 可选依赖或 pdftotext") from None
        reader = PdfReader(pdf)
        if len(reader.pages) > 300:
            raise ProtocolError("PDF 页数超过全文核验上限")
        text.write_text("\n".join(page.extract_text() or "" for page in reader.pages), encoding="utf-8")
        engine = "pypdf"
    content = text.read_text(encoding="utf-8", errors="replace").strip()
    if len(content) < 200:
        raise ProtocolError("PDF 全文提取失败或内容不足")
    return {"pdf": str(pdf), "pdf_sha256": file_hash(pdf), "text": str(text),
            "text_sha256": file_hash(text), "source_url": source_url, "extractor": engine,
            "content": content[:60000]}


def fetch_paper_text(paper, output_dir, paper_id):
    queue = pdf_candidates(paper)
    doi = str(paper.get("doi") or "").removeprefix("https://doi.org/")
    # Crossref sometimes exposes public full-text links absent from normalized search results.
    if doi.startswith("10.") and "/" in doi:
        queue.append("https://api.crossref.org/works/" + quote(doi, safe=""))
    visited, attempts = set(), []
    while queue and len(visited) < 8:
        url = queue.pop(0)
        if url in visited or not public_https(url):
            continue
        visited.add(url)
        try:
            final_url, data = download(url)
            if data.startswith(b"%PDF-"):
                result = extract_pdf(data, output_dir, paper_id, final_url)
                result["resolution_attempts"] = attempts + [{"url": url, "status": "pdf"}]
                return result
            if urlparse(url).hostname == "api.crossref.org":
                metadata = json.loads(data).get("message", {})
                links = [v.get("URL") for v in metadata.get("link", [])
                         if isinstance(v, dict) and v.get("content-type") == "application/pdf"
                         and v.get("intended-application") != "similarity-checking"]
            else:
                parser = PdfLinks(final_url)
                parser.feed(data.decode("utf-8", errors="replace"))
                links = parser.links
            queue[:0] = [link for link in links if public_https(link) and link not in visited]
            attempts.append({"url": url, "resolved_url": final_url, "status": "landing_page",
                             "pdf_links": links})
        except Exception as error:
            attempts.append({"url": url, "status": "failed", "error_type": type(error).__name__,
                             "http_status": getattr(error, "code", None), "message": str(error)[:500]})
    error = ProtocolError("未取得可解析的公开 PDF；保留摘要级状态")
    error.attempts = attempts
    raise error
