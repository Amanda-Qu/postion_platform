"""Conservative local extraction. Unknown data remains unknown; OCR never invents facts."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile

from pydantic import BaseModel, Field


MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_CHARS = 500_000
MAX_DOCX_EXPANDED_BYTES = 48 * 1024 * 1024
MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024


class ParsedJob(BaseModel):
    company: str = ""
    title: str = ""
    city: str = ""
    salary_raw: str = ""
    experience: str = ""
    education: str = ""
    responsibilities: str = ""
    requirements: str = ""
    published_at: str = ""
    status_validity: str = "未知"
    uncertain_fields: list[str] = Field(default_factory=list)


def merge_texts(texts: list[str]) -> str:
    """Deduplicate overlapping screenshot lines without collapsing conflicting facts.

    Compare Unicode-normalized lines with whitespace removed. Punctuation remains
    significant: signs, decimal points, times, slashes and percentages carry facts.
    For example,
    two screenshots both containing '负责：模型训练' produce one line, while
    '薪资 30-60K' and '薪资 40-60K' are both retained for human review. No fuzzy
    edit-distance deduplication is used because a one-digit difference is material.
    """
    seen: set[str] = set()
    output: list[str] = []
    for text in texts:
        for line in (text or "").replace("\r", "\n").splitlines():
            line = unicodedata.normalize("NFKC", line).strip()
            # Windows OCR inserts word spaces in Chinese; removing only spaces
            # between Chinese characters restores labels without joining English.
            line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
            if not line:
                continue
            key = re.sub(r"\s", "", line).casefold()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            output.append(line)
    return "\n".join(output)


def salary_group(raw: str, target: int = 40000) -> dict:
    """Classify only identifiable fixed monthly salary, keeping bonus months separate.

    30–60K·14薪 => 30,000–60,000/month, extra_months=2, 可能符合.
    60–90万/年 => 信息不足; annual compensation is never divided by twelve.
    """
    result = {"group": "信息不足", "min_monthly": None, "max_monthly": None,
              "extra_months": None, "notes": "未能确认税前固定月薪，请沟通确认。"}
    s = unicodedata.normalize("NFKC", raw or "").lower().replace(",", "")
    months = re.search(r"(?:[·•x×*]|\b)(\d{2})\s*薪", s)
    if not months:
        months = re.search(r"(\d{2})\s*薪", s)
    if months:
        result["extra_months"] = max(0, int(months.group(1)) - 12)
    # Prefer an explicitly separated base amount over a monthly combined figure.
    # The first amount in “底薪20K+奖金30K” is 20K; '+' is a separator here,
    # while “40K+” alone is an open lower bound. Never add bonus to base salary.
    explicit = re.search(r"(固定月薪|税前月薪|基本月薪|月基本工资|基本工资|底薪)\s*[:：]?\s*([^\n;；。]+)", s)
    if not explicit:
        explicit = re.search(r"(月薪)\s*[:：]?\s*([^\n;；。]+)", s)
    candidate = explicit.group(2) if explicit else s
    candidate = re.split(r"(?:\+|加|另有|另加|另外|及|以及|外加)\s*(?:年度)?(?:奖金|绩效|提成|股权|股票|期权|补贴)", candidate, maxsplit=1)[0].strip()
    monthly_context = bool((explicit and "月" in explicit.group(1)) or re.search(r"/月|每月|\bmonth(?:ly)?\b|per\s*month", candidate))
    if re.search(r"税后|到手", s) or re.search(r"综合薪|综合收入|含(?:奖金|绩效|提成)|包含|包括|日薪|时薪|/小时|/天|/日|\b(?:hour|hourly|day|daily|week|weekly)\b", candidate):
        result["notes"] = "薪资口径不是已确认的税前固定月薪。"
        return result
    if re.search(r"年薪|年包|/年|每年|万元?\s*/?年|annual|\byear(?:ly)?\b|per\s*year|\bp\.?a\.?\b|总包|package|ote", candidate):
        result["notes"] = "仅有年薪/年包或总包，不能换算为固定月薪。"
        return result
    if not explicit and re.search(r"奖金|绩效|提成|股权|股票|期权", candidate):
        result["notes"] = "金额涉及奖金或浮动报酬，固定月薪需另行确认。"
        return result
    if re.search(r"美元|港币|欧元|usd|hkd|eur|\b(?:gbp|cad|aud|sgd|jpy|chf)\b|\$|€", candidate):
        result["notes"] = "币种不是已确认的人民币，需单独核实。"
        return result
    # JSON-LD baseSalary is preserved by sources as e.g.
    # “CNY 40000–60000 / MONTH”. Bare numbers become yuan only when the
    # entire expression names RMB/CNY and an explicit monthly period. Unknown
    # periods, annual amounts and missing bounds never enter this conversion.
    structured_monthly = re.fullmatch(
        r"(?:cny|rmb)\s*(\d+(?:\.\d+)?)(?:\s*[-–—~～至]\s*(\d+(?:\.\d+)?))?"
        r"\s*(?:/?\s*month(?:ly)?|per\s+month)\s*", candidate)
    if structured_monthly:
        low, high = structured_monthly.groups()
        candidate = f"{low}-{high}元" if high else f"{low}元"
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k|千|万|元)?\s*[-–—~～至]\s*(\d+(?:\.\d+)?)\s*(k|千|万|元)", candidate)
    if match:
        if re.search(r"以下|以内|上限|最高|不超过|低于|少于|<|≤|以上|起", candidate):
            result["notes"] = "薪资区间同时包含上下界限定词，口径需人工确认。"
            return result
        low, unit1, high, unit2 = match.groups()
        factor = {"k": 1000, "千": 1000, "万": 10000, "元": 1}
        minimum = round(float(low) * factor[unit1 or unit2])
        maximum = round(float(high) * factor[unit2])
    else:
        match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(k|千|万|元)(?![a-z])", candidate)
        if not match:
            return result
        # An unqualified number in 万 is usually annual and requires monthly context.
        if match.group(2) == "万" and not monthly_context:
            return result
        minimum = maximum = round(float(match.group(1)) * {"k": 1000, "千": 1000, "万": 10000, "元": 1}[match.group(2)])
        if re.search(r"以下|以内|上限|最高|不超过|低于|少于|<|≤", candidate):
            maximum, minimum = minimum, None
        elif re.match(r"\s*(?:起|以上|\+(?:\s*$|\s*[·•]))", candidate[match.end():]):
            maximum = None
    if "万" in candidate and not monthly_context:
        return result
    if re.search(r"(^|[:：(（])\s*[-−]\s*\d|\d\s*[-–—~～至]\s*[-−]\s*\d", candidate):
        result["notes"] = "薪资包含异常负数，请人工核对。"
        return result
    if minimum is not None and maximum is not None and minimum > maximum:
        result["notes"] = "薪资区间上下限异常，请人工核对。"
        return result
    result.update(min_monthly=minimum, max_monthly=maximum)
    if minimum is not None and minimum >= target:
        result["group"] = "明确符合"
    elif maximum is not None and maximum <= target and re.search(r"低于|少于|<(?![=])", candidate):
        result["group"] = "低于目标"
    elif maximum is None or maximum >= target:
        result["group"] = "可能符合"
    else:
        result["group"] = "低于目标"
    result["notes"] = "按公开月薪范围比较；额外薪数、奖金和股权未计入固定月薪。"
    if result["group"] == "可能符合":
        result["notes"] += "区间覆盖目标，实际固定月薪需沟通确认。"
    return result


def parse_job(text: str) -> dict:
    """Extract explicit labels first; highlight heuristic/absent fields for review."""
    text = merge_texts([text])
    lines = text.splitlines()
    result = ParsedJob()
    aliases = {
        "company": r"公司(?:名称)?|企业(?:名称)?|company",
        "title": r"岗位(?:名称)?|职位(?:名称)?|招聘岗位|job\s*title|title",
        "city": r"工作(?:城市|地点)|城市|地点|location",
        "salary_raw": r"薪资(?:待遇|范围)?|薪酬(?:范围)?|salary",
        "experience": r"工作经验|经验(?:要求)?|experience",
        "education": r"学历(?:要求)?|education",
    }
    explicit: set[str] = set()
    for field, pattern in aliases.items():
        match = re.search(r"^(?:" + pattern + r")\s*[:：]\s*(.+)$", text, re.I | re.M)
        if match:
            setattr(result, field, match.group(1).strip())
            explicit.add(field)
    if not result.company:
        company = next((line for line in lines if re.fullmatch(r".{2,60}(?:有限责任公司|股份有限公司|有限公司|科技公司)", line)), "")
        result.company = company
    if not result.title:
        # A short header with a role word is only a candidate, never a confirmed title.
        result.title = next((line for line in lines[:8] if len(line) < 60 and re.search(r"工程师|研究员|科学家|算法|研发|scientist|engineer", line, re.I) and not re.match(r"[\d一二三四五]+[.、)]|负责|要求|熟悉|精通", line)), "")
    if not result.city:
        match = re.search(r"深圳|北京|上海|杭州|广州|成都|南京|武汉|苏州|西安|香港|远程|remote", text, re.I)
        result.city = match.group(0) if match else ""
    if not result.salary_raw:
        match = re.search(r"[^\n]*(?:\d+(?:\.\d+)?\s*[-–—~～至]\s*\d+(?:\.\d+)?\s*[kK万千元]|薪资面议|薪酬面议|年薪|年包)[^\n]*", text)
        result.salary_raw = match.group(0).strip() if match else ""
    if not result.experience:
        match = re.search(r"\d+\s*[-–—~～至]\s*\d+\s*年(?:以上)?(?:经验)?|\d+\s*年(?:以上)?(?:工作)?经验|经验不限", text)
        result.experience = match.group(0) if match else ""
    if not result.education:
        match = re.search(r"(?:博士|硕士|本科|大专)(?:及以上|以上|学历)?|学历不限", text)
        result.education = match.group(0) if match else ""
    current = None
    buckets: dict[str, list[str]] = {"responsibilities": [], "requirements": []}
    for line in lines:
        heading = re.match(r"^(?:[一二三四五\d]+[、.．)]\s*)?(岗位职责|工作职责|职位描述|工作内容|职责|responsibilities|任职要求|职位要求|岗位要求|任职资格|技能要求|要求|qualifications|requirements)\s*[:：]?\s*(.*)$", line, re.I)
        if heading:
            current = "requirements" if re.search(r"要求|资格|qualifications|requirements", heading.group(1), re.I) else "responsibilities"
            if heading.group(2):
                buckets[current].append(heading.group(2))
        elif re.match(r"^(福利|公司介绍|薪资|薪酬|工作地点|联系方式|投递方式)\s*[:：]", line):
            current = None
        elif current:
            buckets[current].append(line)
    for field, values in buckets.items():
        setattr(result, field, "\n".join(values))
        if values:
            explicit.add(field)
    published = re.search(r"(?:发布时间|发布日期|posted(?:\s+on)?)\s*[:：]?\s*(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", text, re.I)
    if published:
        from datetime import date
        parts = [int(part) for part in re.findall(r"\d+", published.group(1))]
        try:
            result.published_at = date(*parts).isoformat()
            explicit.add("published_at")
        except ValueError:
            # Invalid calendar dates stay unknown; retain the submitted text as
            # evidence instead of silently rolling it into another month/day.
            result.published_at = ""
    result.uncertain_fields = [field for field in aliases if field not in explicit]
    result.uncertain_fields += [field for field in ("responsibilities", "requirements", "published_at") if field not in explicit]
    return result.model_dump()


_WINRT_OCR = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$awaiter = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await-Result($Operation, $ResultType) {
  $task = $awaiter.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.Wait()
  return $task.Result
}
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:JOB_OCR_PATH)) ([Windows.Storage.StorageFile])
$stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
$preferred = $available | Where-Object { $_ -match '^zh' } | Select-Object -First 1
if (-not $preferred) { $preferred = $available | Select-Object -First 1 }
if (-not $preferred) { throw 'OCR_LANGUAGE_MISSING' }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language($preferred)))
$result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
@{ text = (($result.Lines | ForEach-Object { $_.Text }) -join "`n"); language = $preferred } | ConvertTo-Json -Compress
$stream.Dispose()
'''


def _extract_image(path: Path) -> dict:
    from PIL import Image, ImageOps
    warnings = ["截图识别可能存在错字或遗漏，请对照原件核对所有关键字段。"]
    try:
        with Image.open(path) as original:
            if original.width * original.height > 40_000_000:
                return {"text": "", "warnings": ["图片过大，请压缩到 4000 万像素以内。"], "method": "unavailable"}
            image = ImageOps.exif_transpose(original).convert("RGB")
            image.thumbnail((2500, 2500))
            with tempfile.TemporaryDirectory(prefix="job-ocr-") as temp:
                safe_path = Path(temp) / "source.png"
                image.save(safe_path)
                if os.name == "nt":
                    # Encode only our fixed script; the user filename is passed in the
                    # environment, never interpolated into executable PowerShell.
                    encoded = base64.b64encode(_WINRT_OCR.encode("utf-16-le")).decode("ascii")
                    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                        env={**os.environ, "JOB_OCR_PATH": str(safe_path.resolve())}, capture_output=True, timeout=60)
                    if proc.returncode == 0:
                        data = json.loads(proc.stdout.decode("utf-8-sig").strip())
                        if not str(data.get("language", "")).startswith("zh"):
                            warnings.append("本机未启用中文 OCR 语言，中文内容可能无法正确识别。")
                        extracted = data.get("text", "")
                        # A printed dash is often recognised as the Chinese 一.
                        # Correct only a numeric salary range ending in a unit;
                        # preserve the source screenshot and require verification.
                        fixed = re.sub(r"(?<=\d)\s*一\s*(?=\d+(?:\.\d+)?\s*[kK千万元])", "-", extracted)
                        if fixed != extracted:
                            warnings.append("工资区间中的 OCR 字符“一”已按数字连字符规范化，请核对原图。")
                        extracted = fixed
                        if not extracted:
                            warnings.append("未识别到文字，请补充可复制的岗位描述。")
                        return {"text": extracted, "warnings": warnings, "method": "windows-ocr"}
                tesseract = shutil.which("tesseract")
                if tesseract:
                    langs = subprocess.run([tesseract, "--list-langs"], capture_output=True, timeout=10).stdout.decode(errors="replace")
                    lang = "chi_sim+eng" if "chi_sim" in langs else "eng"
                    proc = subprocess.run([tesseract, str(safe_path), "stdout", "-l", lang], capture_output=True, timeout=60)
                    if proc.returncode == 0:
                        if lang == "eng":
                            warnings.append("Tesseract 未配置中文语言包，中文字段需人工核对。")
                        return {"text": proc.stdout.decode("utf-8"), "warnings": warnings, "method": "tesseract"}
        return {"text": "", "warnings": ["截图识别待配置：Windows OCR 不可用或未安装 OCR 语言包。请安装语言包/Tesseract，或粘贴岗位文字。"], "method": "unavailable"}
    except Exception as exc:
        return {"text": "", "warnings": [f"截图识别失败（{type(exc).__name__}），原件已保留，请补充文字。"], "method": "failed"}


def _checked_text(text: str, warnings: list[str], method: str) -> dict:
    if len(text) > MAX_EXTRACTED_CHARS:
        return {"text": "", "warnings": ["提取文字超过 50 万字符，请裁剪文件后重试。"], "method": "unavailable"}
    return {"text": text, "warnings": warnings, "method": method}


def _check_docx_archive(path: Path) -> None:
    """Inspect ZIP metadata before python-docx inflates XML or embedded objects."""
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(info.file_size for info in entries) > MAX_DOCX_EXPANDED_BYTES:
            raise ValueError("DOCX 解压后的文件数量或大小超限")
        for info in entries:
            if info.file_size > 16 * 1024 * 1024:
                raise ValueError("DOCX 单个内部文件超过 16 MB")
            if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 100:
                raise ValueError("DOCX 压缩比异常，拒绝解压")
            if info.flag_bits & 1:
                raise ValueError("DOCX 包含加密内容")


def _extract_pdf(path: Path) -> dict:
    import pypdf
    # Context-local configuration prevents one request from weakening another.
    # Older pypdf releases cannot provide all these limits, so fail closed with
    # an actionable dependency message rather than parse an untrusted PDF.
    if not hasattr(pypdf, "apply_configuration"):
        return {"text": "", "warnings": ["PDF 提取待配置：请升级 pypdf 至 6.19 或更新版本以启用安全解压限制。"], "method": "unavailable"}
    with pypdf.apply_configuration(
        maximum_declared_stream_length=MAX_FILE_BYTES,
        array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
        zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
        zlib_maximum_recovery_input_length=100_000,
        lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
        run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
        jbig2_maximum_output_length=MAX_PDF_STREAM_BYTES,
        image_maximum_buffer_size=MAX_PDF_STREAM_BYTES,
        page_tree_maximum_entries=1000, page_tree_maximum_depth=30,
        xform_maximum_invocations_per_extraction=200, jbig2dec_binary=None,
    ):
        reader = pypdf.PdfReader(str(path), root_object_recovery_limit=1000)
        if reader.is_encrypted:
            return {"text": "", "warnings": ["PDF 已加密，请上传未加密版本或粘贴文字。"], "method": "unavailable"}
        if len(reader.pages) > 100:
            return {"text": "", "warnings": ["PDF 超过 100 页，请仅保留岗位或简历相关页面。"], "method": "unavailable"}
        pages = []
        text_length = 0
        stream_length = 0
        for page in reader.pages:
            # Validate content streams even on malformed pages without fonts;
            # extract_text may return early before decompressing such a stream.
            contents = page.get_contents()
            if contents is not None:
                stream_length += len(contents.get_data())
                if stream_length > MAX_DOCX_EXPANDED_BYTES:
                    raise ValueError("PDF 页面内容解压总量超过 48 MB")
            text = page.extract_text() or ""
            text_length += len(text)
            if text_length > MAX_EXTRACTED_CHARS:
                return {"text": "", "warnings": ["PDF 提取文字超过 50 万字符，请裁剪后重试。"], "method": "unavailable"}
            pages.append(text)
        warnings = []
        empty_pages = [i + 1 for i, text in enumerate(pages) if not text.strip()]
        if empty_pages:
            warnings.append(f"第 {', '.join(map(str, empty_pages))} 页没有可提取文字，可能是扫描页；请转成截图上传进行 OCR。")
        return _checked_text("\n".join(pages).strip(), warnings, "pypdf")


def extract_file(path: Path) -> dict:
    """Read supported files. Empty/scanned PDFs are surfaced, not reported as parsed."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {"text": "", "warnings": ["单个文件超过 12 MB，请压缩或裁剪后重试。"], "method": "unavailable"}
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}:
            return _extract_image(path)
        if suffix == ".docx":
            from docx import Document
            _check_docx_archive(path)
            doc = Document(path)
            chunks = [p.text for p in doc.paragraphs]
            chunks += [" | ".join(cell.text for cell in row.cells) for table in doc.tables for row in table.rows]
            return _checked_text("\n".join(chunks).strip(), [], "python-docx")
        if suffix == ".pdf":
            return _extract_pdf(path)
        if suffix in {".txt", ".md", ".csv", ".json"}:
            raw = path.read_bytes()
            # UTF-16 without a BOM can silently turn valid GB18030 into gibberish.
            encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
            for encoding in encodings:
                try:
                    return _checked_text(raw.decode(encoding), [], "text")
                except UnicodeError:
                    continue
            return _checked_text(raw.decode("utf-8", errors="replace"), ["文件编码未确定，部分字符需要人工核对。"], "text")
        return {"text": "", "warnings": [f"暂不支持 {suffix or '此格式'}，请上传 DOCX、PDF、TXT 或截图。"], "method": "unavailable"}
    except ValueError as exc:
        return {"text": "", "warnings": [f"文件提取受限：{exc}。请裁剪文件或补充文字。"], "method": "unavailable"}
    except Exception as exc:
        return {"text": "", "warnings": [f"文件提取失败（{type(exc).__name__}），请检查文件或补充文字。"], "method": "failed"}
