"""教育部幼兒園公開資料爬蟲服務。

從 https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx 爬取高雄市
所有幼兒園的設立別、地址、電話、核准設立日期、負責人、核定人數、
準公共幼兒園狀態及全園總面積，儲存至 competitor_school 表。

注意：教育部網站 2024 年後改版，資料直接嵌入搜尋結果 GridView，
不再提供個別幼兒園詳細頁（舊版 schno URL 已失效）。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import threading
import time
from datetime import datetime
from typing import Optional

import json as _json

import requests
import urllib3

# 教育部網站使用台灣政府 GRCA 根憑證，不在 certifi CA bundle 中，
# 需停用 SSL 驗證；此為已知政府網站，風險可接受。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from models.base import session_scope
from models.recruitment import CompetitorSchool, RecruitmentSyncState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------

SEARCH_URL = "https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx"
PUNISH_URL = "https://kiang.github.io/ap.ece.moe.edu.tw/punish_all.json"
KIANG_PRESCHOOLS_URL = "https://kiang.github.io/ap.ece.moe.edu.tw/preschools.json"
REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.8  # 每次請求間隔（秒），避免對政府網站造成壓力
MAX_PAGES = 200  # 安全上限（高雄市約 400 所，每頁 10 筆約 40 頁）
PROVIDER_NAME = "moe_ece"
PROVIDER_LABEL = "教育部幼兒園查詢系統（高雄市）"
TARGET_CITY = "高雄市"
TARGET_CITY_CODE = "18"  # 教育部網站高雄市縣市代碼

_SYNC_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Regex 工具
# ---------------------------------------------------------------------------

_VIEWSTATE_RE = re.compile(
    r'<input[^>]+id="__VIEWSTATE"[^>]+value="([^"]*)"', re.IGNORECASE
)
_VIEWSTATE_GEN_RE = re.compile(
    r'<input[^>]+id="__VIEWSTATEGENERATOR"[^>]+value="([^"]*)"', re.IGNORECASE
)
_EVENTVALIDATION_RE = re.compile(
    r'<input[^>]+id="__EVENTVALIDATION"[^>]+value="([^"]*)"', re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<script\b.*?>.*?</script>")
_STYLE_RE = re.compile(r"(?is)<style\b.*?>.*?</style>")


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------


def _strip_tags(text: str) -> str:
    """移除 HTML 標籤並還原 HTML 實體。"""
    text = _SCRIPT_RE.sub("", text)
    text = _STYLE_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def _get_hidden_fields(html_text: str) -> dict:
    """從 HTML 中提取 ASP.NET WebForms 隱藏欄位。"""
    fields = {}
    m = _VIEWSTATE_RE.search(html_text)
    if m:
        fields["__VIEWSTATE"] = html.unescape(m.group(1))
    m = _VIEWSTATE_GEN_RE.search(html_text)
    if m:
        fields["__VIEWSTATEGENERATOR"] = html.unescape(m.group(1))
    m = _EVENTVALIDATION_RE.search(html_text)
    if m:
        fields["__EVENTVALIDATION"] = html.unescape(m.group(1))
    return fields


def _span_text(html_text: str, span_id: str) -> Optional[str]:
    """從 HTML 中提取指定 id 的 <span> 或 <a> 標籤的文字內容。"""
    m = re.search(
        rf'id="{re.escape(span_id)}"[^>]*>([^<]*)<',
        html_text,
        re.IGNORECASE,
    )
    if m:
        val = html.unescape(m.group(1)).strip()
        return val if val else None
    return None


def _parse_gridview_schools(html_text: str) -> list[dict]:
    """從搜尋結果頁面解析 GridView 中所有幼兒園資料。

    新版教育部網站（2024 年後）將所有資料直接嵌入 GridView，
    透過 span id 格式 GridView1_lblXxx_N 取得每所幼兒園欄位。
    """
    schools = []

    # 找所有學校列索引（以校名 span 為基準）
    name_matches = re.findall(
        r'id="GridView1_lblSchName_(\d+)"[^>]*>([^<]+)<',
        html_text,
    )
    for idx_str, raw_name in name_matches:
        n = idx_str
        school_name = html.unescape(raw_name).strip()
        if not school_name:
            continue

        s: dict = {"school_name": school_name}

        # 基本欄位（span 文字）
        for span_suffix, key in [
            (f"GridView1_lblCity_{n}", "city"),
            (f"GridView1_lblArea_{n}", "district"),
            (f"GridView1_lblPub_{n}", "school_type"),
            (f"GridView1_lblTel_{n}", "phone"),
            (f"GridView1_lblCharge_{n}", "owner_name"),  # 負責人
            (f"GridView1_lblRegDate_{n}", "approved_date"),
            (f"GridView1_lblGenStd_{n}", "approved_capacity"),
            (f"GridView1_lblStdPub_{n}", "pre_public_type"),
            (f"GridView1_lblTSpace_{n}", "total_area_sqm"),
        ]:
            s[key] = _span_text(html_text, span_suffix)

        # 裁罰情形：
        #   無裁罰 → <span id="GridView1_lblNoPunish_N">無</span>
        #   有裁罰 → 無該 span，改為 <a onclick="...punish_view.aspx...">檢視</a>
        #   punish_view 在 onclick 屬性（id 之前），須從 divBlkAdRight_N 區塊搜尋
        ad_right_start = html_text.find(f"GridView1_divBlkAdRight_{n}")
        ad_right_end = html_text.find(
            f"GridView1_divBlkAdRight_{int(n)+1}", ad_right_start
        )
        ad_right_html = (
            html_text[ad_right_start:ad_right_end] if ad_right_start >= 0 else ""
        )

        no_punish_span = _span_text(html_text, f"GridView1_lblNoPunish_{n}")
        has_punish_link = "punish_view.aspx" in ad_right_html

        if has_punish_link:
            s["penalty_text"] = "有"  # MOE 直接標記有裁罰
        else:
            s["penalty_text"] = no_punish_span  # "無" 或 None

        # 地址：取 <a id="GridView1_hlAddr_N"> 的文字內容
        m_addr = re.search(
            rf'id="GridView1_hlAddr_{n}"[^>]*>([^<]+)<',
            html_text,
            re.IGNORECASE,
        )
        if m_addr:
            addr = html.unescape(m_addr.group(1)).strip()
            # 去除郵遞區號前綴，如 [807]
            addr = re.sub(r"^\[\d+\]", "", addr).strip()
            s["address"] = addr if addr else None
        else:
            s["address"] = None

        # 網址：取 <a id="GridView1_hlUrl_N"> 的 href
        m_url = re.search(
            rf'id="GridView1_hlUrl_{n}"[^>]+href="([^"]*)"',
            html_text,
            re.IGNORECASE,
        )
        if m_url:
            url = html.unescape(m_url.group(1)).strip()
            s["website"] = (
                url if url and url not in ("http://", "https://", "") else None
            )
        else:
            s["website"] = None

        schools.append(s)

    return schools


def _has_next_page(html_text: str) -> bool:
    """判斷搜尋結果頁面是否存在下一頁按鈕。"""
    return "PageControl1$lbNextPage" in html_text


# ---------------------------------------------------------------------------
# 裁罰資料
# ---------------------------------------------------------------------------


def _fetch_punish_data(sess: requests.Session) -> dict:
    """從 kiang.github.io 取得裁罰記錄 JSON，回傳以負責人/行為人為 key 的字典。
    失敗時回傳空字典，不中斷主流程。"""
    try:
        r = sess.get(PUNISH_URL, timeout=REQUEST_TIMEOUT)
        r.encoding = "utf-8"
        data = _json.loads(r.text)
        if isinstance(data, dict):
            logger.info("[MOE 爬蟲] 裁罰資料載入成功，共 %d 筆紀錄", len(data))
            return data
        logger.warning("[MOE 爬蟲] 裁罰資料格式非預期，略過")
        return {}
    except Exception as e:
        logger.warning(
            "[MOE 爬蟲] 裁罰資料載入失敗，has_penalty 將全部設為 False：%s", e
        )
        return {}


def _owner_has_penalty(punish_data: dict, owner_name: Optional[str]) -> bool:
    """判斷負責人是否有裁罰記錄。"""
    if not punish_data or not owner_name:
        return False
    owner = owner_name.strip()
    return bool(
        punish_data.get(f"負責人：{owner}") or punish_data.get(f"行為人：{owner}")
    )


# ---------------------------------------------------------------------------
# 爬蟲核心
# ---------------------------------------------------------------------------


def _make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        }
    )
    sess.verify = False  # 教育部使用 GRCA 憑證，certifi 不信任，停用驗證
    return sess


def _fetch_search_page(sess: requests.Session) -> Optional[str]:
    """GET 搜尋首頁，取得 __VIEWSTATE 等隱藏欄位。"""
    try:
        r = sess.get(SEARCH_URL, timeout=REQUEST_TIMEOUT)
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.error("[MOE 爬蟲] 無法載入搜尋頁：%s", e)
        return None


def _submit_search(
    sess: requests.Session, hidden_fields: dict, page_target: Optional[str] = None
) -> Optional[str]:
    """POST 搜尋高雄市幼兒園，回傳結果 HTML。

    page_target: 分頁時的 __EVENTTARGET（如 'PageControl1$lbNextPage'），
                 None 表示首次搜尋。
    """
    form_data = {
        **hidden_fields,
        "__EVENTTARGET": page_target or "",
        "__EVENTARGUMENT": "",
        "ddlCityS": TARGET_CITY_CODE,  # 高雄市代碼 "18"
        "ddlAreaS": "",
        "txtKeyNameS": "",
        "ChidlSvc": "rdChildSvc0",
    }
    if not page_target:
        form_data["btnSearch"] = "搜尋"

    try:
        r = sess.post(SEARCH_URL, data=form_data, timeout=REQUEST_TIMEOUT)
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.error("[MOE 爬蟲] POST 查詢失敗：%s", e)
        return None


# ---------------------------------------------------------------------------
# 同步狀態管理
# ---------------------------------------------------------------------------


def _update_sync_state(
    status: str, message: str = "", counts: Optional[dict] = None
) -> None:
    try:
        with session_scope() as sess:
            state = (
                sess.query(RecruitmentSyncState)
                .filter_by(provider_name=PROVIDER_NAME)
                .first()
            )
            if not state:
                state = RecruitmentSyncState(
                    provider_name=PROVIDER_NAME,
                    provider_label=PROVIDER_LABEL,
                )
                sess.add(state)
            state.last_sync_status = status
            state.last_sync_message = message
            if counts is not None:
                import json

                state.last_sync_counts = json.dumps(counts, ensure_ascii=False)
            if status == "running":
                state.sync_in_progress = True
                state.last_started_at = datetime.now()
            else:
                state.sync_in_progress = False
                if status == "success":
                    state.last_synced_at = datetime.now()
            state.updated_at = datetime.now()
    except Exception as e:
        logger.error("[MOE 爬蟲] 更新同步狀態失敗：%s", e)


def get_sync_status() -> dict:
    """查詢目前同步狀態。"""
    try:
        with session_scope() as sess:
            state = (
                sess.query(RecruitmentSyncState)
                .filter_by(provider_name=PROVIDER_NAME)
                .first()
            )
            if not state:
                return {"provider": PROVIDER_NAME, "status": "never_synced"}
            import json

            counts = {}
            if state.last_sync_counts:
                try:
                    counts = json.loads(state.last_sync_counts)
                except Exception:
                    pass
            return {
                "provider": PROVIDER_NAME,
                "provider_label": state.provider_label,
                "sync_in_progress": state.sync_in_progress,
                "last_started_at": (
                    state.last_started_at.isoformat() if state.last_started_at else None
                ),
                "last_synced_at": (
                    state.last_synced_at.isoformat() if state.last_synced_at else None
                ),
                "last_sync_status": state.last_sync_status,
                "last_sync_message": state.last_sync_message,
                "counts": counts,
            }
    except Exception as e:
        logger.error("[MOE 爬蟲] 查詢同步狀態失敗：%s", e)
        return {"provider": PROVIDER_NAME, "status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# kiang preschools.json 補充同步
# ---------------------------------------------------------------------------


def _sync_kiang_supplementary(http_sess: requests.Session, db_session) -> int:
    """從 kiang preschools.json 補充 monthly_fee、面積、接駁等欄位到 competitor_school。

    只更新 DB 為 null 的欄位，不覆蓋已有資料。回傳更新筆數。
    """
    try:
        r = http_sess.get(KIANG_PRESCHOOLS_URL, timeout=30)
        r.encoding = "utf-8"
        geojson = _json.loads(r.text)
    except Exception as e:
        logger.warning("[kiang 同步] 下載 preschools.json 失敗：%s", e)
        return 0

    features = geojson.get("features", [])
    if not features:
        return 0

    # 篩選高雄市
    kaohsiung_features = []
    for f in features:
        props = f.get("properties", {})
        city = props.get("city", "")
        if "高雄" in city:
            kaohsiung_features.append(props)

    logger.info("[kiang 同步] 高雄市共 %d 筆", len(kaohsiung_features))

    # 建立 DB 學校 lookup：{正規化名稱: CompetitorSchool}
    all_schools = (
        db_session.query(CompetitorSchool)
        .filter(
            CompetitorSchool.is_active == True,  # noqa: E712
            CompetitorSchool.city.like("%高雄%"),
        )
        .all()
    )
    school_by_name: dict[str, Any] = {}
    for s in all_schools:
        key = (
            (s.school_name or "")
            .replace("臺", "台")
            .replace("幼稚園", "幼兒園")
            .strip()
            .lower()
        )
        school_by_name[key] = s

    updated = 0
    now = datetime.now()
    for props in kaohsiung_features:
        title = (props.get("title") or "").strip()
        if not title:
            continue
        key = title.replace("臺", "台").replace("幼稚園", "幼兒園").strip().lower()
        school = school_by_name.get(key)
        if not school:
            continue

        changed = False
        # monthly_fee：DB 為 null 時才用 kiang 值
        kiang_fee = props.get("monthly")
        if school.monthly_fee is None and kiang_fee:
            try:
                school.monthly_fee = int(kiang_fee)
                changed = True
            except (ValueError, TypeError):
                pass

        # 面積
        if school.indoor_area_sqm is None and props.get("size_in"):
            try:
                school.indoor_area_sqm = float(props["size_in"])
                changed = True
            except (ValueError, TypeError):
                pass
        if school.outdoor_area_sqm is None and props.get("size_out"):
            try:
                school.outdoor_area_sqm = float(props["size_out"])
                changed = True
            except (ValueError, TypeError):
                pass

        # 樓層
        if school.floor_info is None and props.get("floor"):
            school.floor_info = str(props["floor"])[:255]
            changed = True

        # 接駁車
        if school.shuttle_info is None and props.get("shuttle"):
            school.shuttle_info = str(props["shuttle"])[:255]
            changed = True

        # 課後照顧
        if not school.has_after_school and props.get("is_after"):
            school.has_after_school = True
            changed = True

        if changed:
            school.kiang_synced_at = now
            updated += 1

    if updated:
        db_session.flush()

    return updated


# ---------------------------------------------------------------------------
# 主同步函式
# ---------------------------------------------------------------------------


def sync_moe_kindergartens() -> dict:
    """
    爬取教育部高雄市幼兒園資料並 upsert 至 competitor_school 表。

    以 threading.Lock 確保不會同時執行兩次。
    回傳統計資訊 dict：{ created, updated, failed, total_pages }
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        logger.warning("[MOE 爬蟲] 已有同步作業在執行，跳過本次")
        return {"status": "already_running"}

    try:
        _update_sync_state("running", "開始爬取教育部高雄市幼兒園資料")
        sess_http = _make_session()

        # Step 1: 載入首頁取得隱藏欄位
        home_html = _fetch_search_page(sess_http)
        if not home_html:
            _update_sync_state("error", "無法載入教育部搜尋首頁")
            return {"status": "error", "message": "無法載入搜尋首頁"}

        hidden = _get_hidden_fields(home_html)
        if not hidden.get("__VIEWSTATE"):
            _update_sync_state("error", "找不到 __VIEWSTATE，網站結構可能已改變")
            return {"status": "error", "message": "找不到 __VIEWSTATE"}

        # Step 2: POST 搜尋高雄市
        time.sleep(REQUEST_DELAY)
        result_html = _submit_search(sess_http, hidden)
        if not result_html:
            _update_sync_state("error", "POST 搜尋失敗")
            return {"status": "error", "message": "POST 搜尋失敗"}

        # Step 3: 載入裁罰資料（一次性，後續逐所比對）
        punish_data = _fetch_punish_data(sess_http)

        created = 0
        updated = 0
        failed = 0
        total_pages = 0
        current_html = result_html

        # Step 4: 逐頁解析 GridView，直接 upsert（不需爬取個別詳細頁）
        for page_num in range(1, MAX_PAGES + 1):
            total_pages = page_num
            schools = _parse_gridview_schools(current_html)
            logger.info("[MOE 爬蟲] 第 %d 頁解析到 %d 所幼兒園", page_num, len(schools))

            if not schools:
                logger.warning("[MOE 爬蟲] 第 %d 頁無資料，停止", page_num)
                break

            with session_scope() as db:
                for s in schools:
                    school_name = s.get("school_name")
                    if not school_name:
                        failed += 1
                        continue
                    try:
                        city = s.get("city") or TARGET_CITY
                        owner_name = s.get("owner_name")

                        # has_penalty：MOE 直接顯示「無」或其他；再用 kiang 補強
                        penalty_text = (s.get("penalty_text") or "").strip()
                        has_penalty = penalty_text not in (
                            "無",
                            "",
                        ) or _owner_has_penalty(punish_data, owner_name)

                        # 核定人數 → int
                        capacity: Optional[int] = None
                        if s.get("approved_capacity"):
                            m = re.search(r"(\d+)", s["approved_capacity"])
                            capacity = int(m.group(1)) if m else None

                        # 全園總面積 → float
                        area: Optional[float] = None
                        if s.get("total_area_sqm"):
                            m = re.search(r"([\d,]+(?:\.\d+)?)", s["total_area_sqm"])
                            if m:
                                try:
                                    area = float(m.group(1).replace(",", ""))
                                except ValueError:
                                    pass

                        # 準公共幼兒園「無」→ None
                        pre_public = s.get("pre_public_type")
                        if pre_public in ("無", ""):
                            pre_public = None

                        now = datetime.now()
                        school_id = hashlib.md5(
                            f"{city}{school_name}".encode("utf-8")
                        ).hexdigest()[:8]
                        source_key = f"moe_ece:{school_id}"

                        # 用 school_name 比對（新版網站無法取得舊版 schNo）
                        existing = (
                            db.query(CompetitorSchool)
                            .filter_by(school_name=school_name)
                            .first()
                        )

                        if existing:
                            existing.owner_name = owner_name or existing.owner_name
                            existing.school_type = (
                                s.get("school_type") or existing.school_type
                            )
                            existing.pre_public_type = (
                                pre_public or existing.pre_public_type
                            )
                            existing.phone = s.get("phone") or existing.phone
                            existing.address = s.get("address") or existing.address
                            existing.district = s.get("district") or existing.district
                            existing.city = city
                            existing.approved_capacity = (
                                capacity or existing.approved_capacity
                            )
                            existing.approved_date = (
                                s.get("approved_date") or existing.approved_date
                            )
                            existing.total_area_sqm = area or existing.total_area_sqm
                            existing.website = s.get("website") or existing.website
                            existing.has_penalty = has_penalty
                            existing.is_active = True
                            existing.source_updated_at = now
                            existing.updated_at = now
                            updated += 1
                        else:
                            record = CompetitorSchool(
                                source_school_id=school_id,
                                source_key=source_key,
                                school_name=school_name,
                                owner_name=owner_name,
                                school_type=s.get("school_type"),
                                pre_public_type=pre_public,
                                is_active=True,
                                phone=s.get("phone"),
                                address=s.get("address"),
                                district=s.get("district"),
                                city=city,
                                approved_capacity=capacity,
                                approved_date=s.get("approved_date"),
                                total_area_sqm=area,
                                website=s.get("website"),
                                has_penalty=has_penalty,
                                source_updated_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                            db.add(record)
                            created += 1

                    except Exception as e:
                        logger.error("[MOE 爬蟲] 處理 %s 失敗：%s", school_name, e)
                        failed += 1

            if page_num % 5 == 0:
                logger.info(
                    "[MOE 爬蟲] 進度：第 %d 頁（新增 %d，更新 %d，失敗 %d）",
                    page_num,
                    created,
                    updated,
                    failed,
                )

            if not _has_next_page(current_html):
                logger.info("[MOE 爬蟲] 已到最後一頁（第 %d 頁）", page_num)
                break

            hidden = _get_hidden_fields(current_html)
            time.sleep(REQUEST_DELAY)
            next_html = _submit_search(
                sess_http, hidden, page_target="PageControl1$lbNextPage"
            )
            if not next_html:
                logger.warning("[MOE 爬蟲] 第 %d 頁翻頁失敗，停止", page_num + 1)
                break
            current_html = next_html

        counts = {
            "total_pages": total_pages,
            "created": created,
            "updated": updated,
            "failed": failed,
        }
        msg = (
            f"完成：新增 {created}，更新 {updated}，失敗 {failed}，共 {total_pages} 頁"
        )
        _update_sync_state("success", msg, counts)
        logger.info("[MOE 爬蟲] %s", msg)

        # ── kiang 補充同步 ─────────────────────────────────────────────
        try:
            kiang_count = _sync_kiang_supplementary(http_sess, session)
            if kiang_count:
                logger.info("[MOE 爬蟲] kiang 補充同步完成，更新 %d 筆", kiang_count)
        except Exception as _ke:
            logger.warning("[MOE 爬蟲] kiang 補充同步失敗（不影響主流程）：%s", _ke)

        # 清理 30 天以前的暫存原始資料，避免 sync_raw_data 無限膨脹
        try:
            from models.database import get_session as _get_session
            from sqlalchemy import text as _text

            _sess = _get_session()
            try:
                result = _sess.execute(
                    _text(
                        "DELETE FROM sync_raw_data WHERE created_at < NOW() - INTERVAL '30 days'"
                    )
                )
                if result.rowcount > 0:
                    _sess.commit()
                    logger.info(
                        "[MOE 爬蟲] 清理 sync_raw_data 過期資料 %d 筆", result.rowcount
                    )
            finally:
                _sess.close()
        except Exception as _e:
            logger.debug("[MOE 爬蟲] 清理 sync_raw_data 失敗（可忽略）：%s", _e)

        return {"status": "success", **counts}

    except Exception as e:
        logger.exception("[MOE 爬蟲] 同步失敗：%s", e)
        _update_sync_state("error", str(e))
        return {"status": "error", "message": str(e)}
    finally:
        _SYNC_LOCK.release()
