import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st
import threading
import os
import json
import uuid
import glob
from datetime import datetime

# ============================================================
# 1. 기본 설정
# ============================================================
st.set_page_config(page_title="전용면적 조회", page_icon="🏠", layout="wide")

BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

_required_secrets = ["BUILDING_API_KEY", "PERMIT_API_KEY", "JUSO_API_KEY"]
_missing = [k for k in _required_secrets if k not in st.secrets]
if _missing:
    st.error(f"⚠️ Streamlit Secrets 누락: {', '.join(_missing)}")
    st.stop()

BUILDING_API_KEY_RAW = st.secrets["BUILDING_API_KEY"]
BUILDING_API_KEY_DECODED = urllib.parse.unquote(BUILDING_API_KEY_RAW)

PERMIT_API_KEY_RAW = st.secrets["PERMIT_API_KEY"]
PERMIT_API_KEY_DECODED = urllib.parse.unquote(PERMIT_API_KEY_RAW)

JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

DEBUG = st.sidebar.checkbox("🔧 디버그 모드", value=False)

if "uid" not in st.query_params:
    st.query_params["uid"] = uuid.uuid4().hex[:12]
USER_ID = st.query_params["uid"]

if "address_count" not in st.session_state:
    st.session_state.address_count = 1
MAX_ADDRESS = 10

QUERY_DIR = "query_results"
os.makedirs(QUERY_DIR, exist_ok=True)

APARTMENT_BRAND_KEYWORDS = (
    "자이", "푸르지오", "래미안", "힐스테이트", "아이파크", "e편한세상", "편한세상",
    "더샵", "롯데캐슬", "SK뷰", "한신더휴", "한신", "금호어울림", "코오롱하늘채",
    "호반베르디움", "반도유보라", "부영사랑으로", "중흥S클래스", "제일풍경채",
    "대방노블랜드", "우미린", "벽산블루밍", "동원로얄듀크", "KCC스위첸",
    "한화포레나", "포레나", "두산위브", "현대홈타운", "아파트", "APT",
    "포레온", "올림픽파크", "센트라스", "파크",
)
VILLA_LIKE_KEYWORDS = ("빌라", "타운하우스", "빌", "홈타운")

# ============================================================
# 슬롯 유틸
# ============================================================
def get_next_empty_slot():
    for i in range(MAX_ADDRESS):
        val = str(st.session_state.get(f"address_{i}", "") or "").strip()
        if not val:
            return i + 1
    return MAX_ADDRESS

def fill_slot(slot_idx_0based, value):
    st.session_state[f"address_{slot_idx_0based}"] = value
    needed = slot_idx_0based + 1
    if needed > st.session_state.address_count:
        st.session_state.address_count = needed

def slot_label(slot_1based, next_slot_1based):
    is_next = (slot_1based == next_slot_1based)
    has_val = bool(str(st.session_state.get(f"address_{slot_1based - 1}", "") or "").strip())
    if is_next and has_val:
        return f"⭐ 주소 {slot_1based} (다음 빈슬롯 · 덮어쓰기)", True
    if is_next:
        return f"⭐ 주소 {slot_1based} (다음 빈슬롯)", True
    if has_val:
        return f"주소 {slot_1based} ✏️ (덮어쓰기)", False
    return f"주소 {slot_1based}", False

def combine_address(base, detail):
    b = str(base or "").strip()
    d = str(detail or "").strip()
    if not d:
        return b
    return f"{b} {d}"

@st.dialog("📥 주소 넣을 위치 선택")
def slot_picker_dialog(candidate, detail_key, base_key, next_slot_1based):
    st.caption("넣을 위치를 선택하세요. **선택 즉시 반영되고 창이 자동으로 닫힙니다.**")
    base_val = candidate.get(base_key, "")
    detail_val = st.session_state.get(detail_key, "")
    final_val = combine_address(base_val, detail_val)
    st.caption(f"📝 입력될 주소: `{final_val}`")

    for slot_1b in range(1, MAX_ADDRESS + 1):
        label, is_next = slot_label(slot_1b, next_slot_1based)
        if st.button(
            label, key=f"dlg_{base_key}_{slot_1b}",
            use_container_width=True,
            type="primary" if is_next else "secondary",
        ):
            final_value = combine_address(
                candidate.get(base_key, ""),
                st.session_state.get(detail_key, ""),
            )
            fill_slot(slot_1b - 1, final_value)
            st.session_state["_flash_msg"] = f"✅ 주소 {slot_1b}에 입력 완료: {final_value}"
            st.rerun()

# ============================================================
# 파싱 유틸
# ============================================================
def clean_address_for_parsing(address):
    text = str(address).strip()
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\s*\([^()]*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def extract_bldnm_from_parens(address):
    results = []
    for m in re.finditer(r"\(([^()]*)\)", str(address)):
        for tok in re.split(r"[,，]", m.group(1)):
            tok = tok.strip()
            if not tok:
                continue
            if tok.endswith(("동", "읍", "면", "리", "가")):
                continue
            results.append(tok)
    return results

def normalize_name(s):
    if s is None:
        return ""
    return re.sub(r"[동호제\s]", "", str(s).strip())

def normalize_bldnm(s):
    if s is None:
        return ""
    s = re.sub(r"\s+", "", str(s))
    s = s.replace("-", "").replace("_", "")
    return s.lower()

def is_match(target, source):
    t, s = normalize_name(target), normalize_name(source)
    if not t or not s:
        return False
    if t.isdigit() and s.isdigit():
        return int(t) == int(s)
    return t.upper() == s.upper()

def extract_dong_ho(address):
    text = clean_address_for_parsing(address)
    dong = ho = ""
    m = re.search(r"([0-9A-Za-z]+)\s*동(?![가-힣])", text)
    if m:
        dong = m.group(1)
    m = re.search(r"(\d+)\s*호(?![가-힣])", text)
    if m:
        ho = m.group(1)
    if not dong and not ho:
        m = re.search(r"(\d{2,4})-(\d{3,4})\b", text)
        if m:
            dong, ho = m.group(1), m.group(2)
    if not ho:
        toks = text.split()
        if toks and toks[-1].isdigit() and len(toks[-1]) >= 3:
            ho = toks[-1]
    return dong, ho

def extract_road_pattern(address):
    text = str(address).strip()
    m = re.search(r"([가-힣]+(?:로|길|대로)(?:\d+(?:로|길))?)\s*(\d+(?:-\d+)?)", text)
    return (m.group(1), m.group(2)) if m else (None, None)

def sanitize_road_address(address):
    text = clean_address_for_parsing(address)
    text = re.sub(r"\s+[0-9A-Za-z]+\s*동(?![가-힣]).*", "", text)
    text = re.sub(r"\s+\d+\s*호(?![가-힣]).*", "", text)
    road, num = extract_road_pattern(text)
    if road and num:
        m = re.search(re.escape(road) + r"\s*" + re.escape(num), text)
        if m:
            return text[:m.end()].strip()
    return text.strip()

def validate_juso_match(user_input, juso_item):
    ur, un = extract_road_pattern(user_input)
    if not ur or not un:
        return True
    ja = (juso_item.get("roadAddrPart1", "") or "") + " " + (juso_item.get("roadAddrPart2", "") or "")
    jn = ja.replace(" ", "")
    return ur.replace(" ", "") in jn and un in jn

def parse_rel_jibun(rel_jibun_str):
    results = []
    if not rel_jibun_str:
        return results
    for tok in re.split(r"[,，]", rel_jibun_str):
        m = re.search(r"(\d+)(?:-(\d+))?", tok.strip())
        if m:
            results.append((m.group(1), m.group(2) or "0"))
    return results

# ============================================================
# Juso API
# ============================================================
def search_juso_single(keyword):
    params = {"confmKey": JUSO_API_KEY, "currentPage": 1, "countPerPage": 10,
              "keyword": keyword, "resultType": "json", "addInfoYn": "Y"}
    dbg = {"keyword": keyword}
    try:
        res = requests.get(JUSO_API_URL, params=params, timeout=10)
        dbg["status_code"] = res.status_code
        try:
            data = res.json()
        except ValueError:
            return [], "JSON 파싱 실패", dbg
        common = data.get("results", {}).get("common", {})
        if common.get("errorCode", "") not in ("", "0"):
            return [], f"오류 [{common.get('errorCode')}]", dbg
        juso_list = data.get("results", {}).get("juso", [])
        return juso_list, "정상", dbg
    except requests.exceptions.RequestException as e:
        return [], f"요청 실패: {e}", dbg

def _juso_item_to_dict(j):
    admCd = j.get("admCd", "")
    if len(admCd) < 10:
        return None
    bun = str(int(j["lnbrMnnm"])) if j.get("lnbrMnnm", "").isdigit() else "0"
    ji = str(int(j["lnbrSlno"])) if j.get("lnbrSlno", "").isdigit() else "0"
    det = (j.get("detBdNmList") or "").strip()
    return {
        "sigunguCd": admCd[:5], "bjdongCd": admCd[5:10],
        "bun": bun, "ji": ji,
        "platGbCd": "1" if str(j.get("mtYn", "0")) == "1" else "0",
        "jibunAddr": j.get("jibunAddr", ""), "bdNm": j.get("bdNm", ""),
        "roadAddr": j.get("roadAddr", ""),
        "is_single_building": (det == ""),
        "detBdNmList": det, "relJibun": j.get("relJibun", "") or "",
        "buldMnnm": j.get("buldMnnm", "") or "",
        "buldSlno": j.get("buldSlno", "") or "",
        "bjdongCd_alt": j.get("naBjdongCd", "") or "",
    }

def search_juso(address):
    all_dbg = {}
    cleaned = clean_address_for_parsing(address)
    sanitized = sanitize_road_address(address)
    candidates = [("raw", address)]
    if cleaned and cleaned != address:
        candidates.append(("cleaned", cleaned))
    if sanitized and sanitized not in (address, cleaned):
        candidates.append(("sanitized", sanitized))
    last_msg = ""
    for label, kw in candidates:
        juso_list, msg, dbg = search_juso_single(kw)
        all_dbg[label] = dbg
        last_msg = msg
        for j in juso_list:
            if not validate_juso_match(address, j):
                continue
            d = _juso_item_to_dict(j)
            if d:
                d["_matched_by"] = label
                return d, "정상", all_dbg
    return None, f"주소 매칭 실패: {last_msg}", all_dbg

def search_address_candidates(keyword, count=20):
    params = {
        "confmKey": JUSO_API_KEY, "currentPage": 1, "countPerPage": str(count),
        "keyword": keyword, "resultType": "json", "addInfoYn": "Y",
    }
    try:
        res = requests.get(JUSO_API_URL, params=params, timeout=10)
        data = res.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return [], f"요청 실패: {e}"
    common = data.get("results", {}).get("common", {})
    if common.get("errorCode", "") not in ("", "0"):
        return [], f"API 오류 [{common.get('errorCode')}] {common.get('errorMessage','')}"
    juso_list = data.get("results", {}).get("juso", [])
    results = []
    for j in juso_list:
        full_road = j.get("roadAddr", "")
        clean_road = clean_address_for_parsing(full_road)
        results.append({
            "도로명주소": full_road,
            "도로명주소_깔끔": clean_road,
            "지번주소": j.get("jibunAddr", ""),
            "건물명": j.get("bdNm", ""),
            "우편번호": j.get("zipNo", ""),
            "동목록": j.get("detBdNmList", ""),
        })
    return results, None

# ============================================================
# 국토부 API 호출
# ============================================================
def call_api_all_pages(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji,
                       match_check=None, extra_params=None, collect_all=False):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    all_items, page, error_msg, debug_snippets = [], 1, None, []
    ROWS_PER_PAGE, MAX_PAGES = 1000, 40
    total_pages_needed = None
    while True:
        params = {"serviceKey": BUILDING_API_KEY_DECODED,
                  "sigunguCd": sigunguCd, "bjdongCd": bjdongCd,
                  "platGbCd": platGbCd, "bun": str(bun).zfill(4), "ji": str(ji).zfill(4),
                  "numOfRows": str(ROWS_PER_PAGE), "pageNo": str(page), "_type": "json"}
        if extra_params:
            params.update(extra_params)
        try:
            raw_text = ""
            for attempt in range(4):
                try:
                    res = requests.get(url, params=params, timeout=25)
                    raw_text = res.text or ""
                    if raw_text.strip():
                        break
                except requests.exceptions.RequestException:
                    raw_text = ""
                if attempt < 3:
                    time.sleep(2.0 * (attempt + 1))
            if not raw_text.strip():
                error_msg = f"[{endpoint}] 빈 응답 4회"
                break
            try:
                data = res.json()
            except ValueError:
                error_msg = f"[{endpoint}] JSON 파싱 실패"
                break
            header = data.get("response", {}).get("header", {})
            if header.get("resultCode", "") not in ("00", "0", ""):
                error_msg = f"[{endpoint}] API 오류 [{header.get('resultCode')}]"
                break
            body = data.get("response", {}).get("body", {})
            items = body.get("items", {})
            if not items:
                break
            item_list = items.get("item", [])
            if isinstance(item_list, dict):
                item_list = [item_list]
            if not item_list:
                break
            all_items.extend(item_list)
            total_count = int(body.get("totalCount", 0) or 0)
            if total_pages_needed is None:
                total_pages_needed = max(1, -(-total_count // ROWS_PER_PAGE))
            if match_check is not None and not collect_all:
                for it in item_list:
                    if match_check(it):
                        return all_items, error_msg, debug_snippets
            if len(all_items) >= total_count:
                break
            if page >= min(total_pages_needed, MAX_PAGES):
                break
            page += 1
            time.sleep(0.4)
        except requests.exceptions.RequestException as e:
            error_msg = f"[{endpoint}] 요청 실패: {e}"
            break
    return all_items, error_msg, debug_snippets

# ============================================================
# 매칭 엔진
# ============================================================
def _bjdong_variants(bjdongCd, alt=None):
    variants = []
    def _add(v):
        if v and v not in variants:
            variants.append(v)
    _add(bjdongCd)
    if alt:
        _add(alt)
    if bjdongCd and len(bjdongCd) >= 2:
        last2 = bjdongCd[-2:]
        if last2 == "00":
            _add(bjdongCd[:-2] + "01")
        elif last2 == "01":
            _add(bjdongCd[:-2] + "00")
    return variants


def _ho_matches(target_ho, ho_text):
    target = str(target_ho).strip()
    h = str(ho_text or "").strip()
    if not target or not h:
        return False
    if is_match(target, h):
        return True
    for m in re.finditer(r"(\d+)\s*호", h):
        if m.group(1) == target:
            return True
    h_cleaned = h
    for _ in range(3):
        h_cleaned = re.sub(r"\d+\s*(?:층|F|f|FLR|flr|th|st|nd|rd)", " ", h_cleaned)
        h_cleaned = re.sub(r"(?:층|F|f|FLR|flr)\s*\d+", " ", h_cleaned)
    if re.search(rf"(?<!\d){re.escape(target)}(?!\d)", h_cleaned):
        return True
    all_nums = re.findall(r"\d+", h)
    if all_nums and all_nums[-1] == target:
        return True
    tokens = re.split(r"[\s\-_/]+", h)
    for t in tokens:
        if t == target:
            return True
    return False


def _is_exclusive(item):
    cd = str(item.get("exposPubuseGbCd", "")).strip()
    nm = str(item.get("exposPubuseGbCdNm", "")).strip()
    if cd in ("1", "01", "001"):
        return True
    if cd in ("2", "02", "002"):
        return False
    if nm:
        if "공용" in nm:
            return False
        if "전유" in nm:
            return True
    h = str(item.get("hoNm", "")).strip()
    if h and re.search(r"\d{2,}", h) and not re.search(
            r"주차|기계|계단|홀|화장실|창고|관리|경비|지하|옥탑|부속", h):
        return True
    return False


def _dong_matches(target_dong, item_dong):
    t = str(target_dong or "").strip()
    d = str(item_dong or "").strip()
    if not t or not d:
        return True
    if is_match(t, d):
        return True
    if re.match(r"^\d+\s*(?:층|F|f|FLR|flr|th|st|nd|rd)$", d):
        return True
    return False


def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, platGbCd,
                        target_dong, target_ho, juso=None, expected_bldNm=None):
    if not target_ho:
        return None, "호수를 인식하지 못했습니다.", [], [], None
    expected_bldNm = expected_bldNm or []
    all_errors, all_debug = [], []

    def _safe_area(item):
        for field in ("area", "exposArea", "전유면적"):
            raw = item.get(field)
            if raw is None or raw == "":
                continue
            try:
                cleaned = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
                if not cleaned:
                    continue
                v = float(cleaned)
                if 5.0 <= v <= 500.0:
                    return round(v, 2)
            except (ValueError, TypeError):
                continue
        return None

    def _bldnm_ok(item):
        if not expected_bldNm:
            return True
        item_bld = normalize_bldnm(item.get("bldNm") or "")
        if not item_bld:
            return True
        for exp in expected_bldNm:
            en = normalize_bldnm(exp)
            if not en:
                continue
            if en == item_bld or en in item_bld or item_bld in en:
                return True
        return False

    def _matches(item):
        if not _is_exclusive(item):
            return False
        if not _bldnm_ok(item):
            return False
        h = (item.get("hoNm") or "").strip()
        if not _ho_matches(target_ho, h):
            return False
        if target_dong:
            d = (item.get("dongNm") or "").strip()
            if not _dong_matches(target_dong, d):
                return False
        return True

    def _matches_relaxed(item):
        h = (item.get("hoNm") or "").strip()
        if not _ho_matches(target_ho, h):
            return False
        if target_dong:
            d = (item.get("dongNm") or "").strip()
            if not _dong_matches(target_dong, d):
                return False
        return True

    def _matches_final(item):
        h = (item.get("hoNm") or "").strip()
        return _ho_matches(target_ho, h)

    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    bun_ji_candidates = [(bun, ji)]
    if ji != "0":
        bun_ji_candidates.append((bun, "0"))
    seen_bj = set()
    bun_ji_unique = []
    for b, j in bun_ji_candidates:
        key = (str(b), str(j))
        if key not in seen_bj:
            seen_bj.add(key)
            bun_ji_unique.append((b, j))
    bun_ji_candidates = bun_ji_unique
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]

    combos = []
    def _add(pg, bd, b, j):
        key = (str(pg), str(bd), str(b), str(j))
        if key not in combos:
            combos.append(key)
    for bd in bjdong_variants:
        for b, j in bun_ji_candidates:
            _add(platGbCd, bd, b, j)
    for pg in plat_candidates:
        if pg == platGbCd:
            continue
        for bd in bjdong_variants:
            for b, j in bun_ji_candidates:
                _add(pg, bd, b, j)

    filter_attempts = [{"hoNm": str(target_ho)}]
    if target_dong:
        filter_attempts.insert(0, {"dongNm": str(target_dong), "hoNm": str(target_ho)})

    first_combo = combos[0] if combos else None
    if first_combo:
        pg, bd, b, j = first_combo
        for extra in filter_attempts:
            area_list, err1, dbg1 = call_api_all_pages(
                "getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j,
                match_check=_matches, extra_params=extra)
            if err1:
                all_errors.append(err1)
            all_debug.extend(dbg1)
            for a in area_list:
                if _matches(a):
                    val = _safe_area(a)
                    if val is not None:
                        return val, "조회 성공 (필터)", all_errors, all_debug, a

    survey_items = []
    any_data_found = False
    if first_combo:
        pg, bd, b, j = first_combo
        area_list, err1, dbg1 = call_api_all_pages(
            "getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j,
            match_check=None, extra_params=None, collect_all=True)
        if err1:
            all_errors.append(err1)
        all_debug.extend(dbg1)
        if area_list:
            any_data_found = True
            survey_items.extend(area_list)

    if not survey_items:
        for pg, bd, b, j in combos[1:]:
            area_list, err1, dbg1 = call_api_all_pages(
                "getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j,
                match_check=None, extra_params=None, collect_all=True)
            if err1:
                all_errors.append(err1)
            all_debug.extend(dbg1)
            if area_list:
                any_data_found = True
                survey_items.extend(area_list)
                break

    if survey_items:
        for a in survey_items:
            if _matches(a):
                val = _safe_area(a)
                if val is not None:
                    return val, "조회 성공 (전체조회)", all_errors, all_debug, a
        for a in survey_items:
            if _matches_relaxed(a):
                val = _safe_area(a)
                if val is not None:
                    return val, "조회 성공 (완화매칭)", all_errors, all_debug, a
        for a in survey_items:
            if _matches_final(a):
                val = _safe_area(a)
                if val is not None:
                    return val, "조회 성공 (호수만 매칭)", all_errors, all_debug, a

    if not any_data_found:
        return None, f"전유부·표제부 모두 데이터 없음.", all_errors, all_debug, None

    all_hos = sorted({(i.get("hoNm") or "").strip() for i in survey_items
                      if (i.get("hoNm") or "").strip()})
    similar_hos = [h for h in all_hos if _ho_matches(target_ho, h)]
    hint = f"hoNm 예시: {', '.join(all_hos[:10])}"
    if similar_hos:
        hint += f" | ★ 매칭: {similar_hos}"
    status = f"동/호 매칭 실패 | {hint}"
    return None, status, all_errors, all_debug, None


# ============================================================
# 총괄표제부
# ============================================================
def _fetch_recap_title(sigunguCd, bjdongCd, bun, ji, platGbCd, juso=None):
    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]
    all_debug = []
    for bd in bjdong_variants:
        for pg in plat_candidates:
            items, err, dbg = call_api_all_pages(
                "getBrRecapTitleInfo", sigunguCd, bd, pg, bun, ji,
                match_check=None, extra_params=None, collect_all=True)
            all_debug.extend(dbg)
            if items:
                item = items[0]
                return {
                    "지상층수": item.get("grndFlrCnt", "") or "",
                    "세대수": item.get("hhldCnt", "") or item.get("hhlDcnt", "") or "",
                    "호수": item.get("hoCnt", "") or "",
                    "가구수": item.get("fmlyCnt", "") or "",
                    "연면적": item.get("totArea", "") or "",
                    "대지면적": item.get("platArea", "") or "",
                    "건축면적": item.get("archArea", "") or "",
                }, all_debug
    return None, all_debug


# ============================================================
# 인허가정보 API
# ============================================================
def fetch_housing_type_from_permit(sigunguCd, bjdongCd, bun, ji, platGbCd):
    url = f"{BUILDING_API_BASE}/getBrHstpInfo"
    bjdong_variants = _bjdong_variants(bjdongCd)
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]
    all_debug = []
    for bd in bjdong_variants:
        for pg in plat_candidates:
            params = {
                "serviceKey": PERMIT_API_KEY_DECODED,
                "sigunguCd": sigunguCd, "bjdongCd": bd, "platGbCd": pg,
                "bun": str(bun).zfill(4), "ji": str(ji).zfill(4),
                "numOfRows": "100", "pageNo": "1", "_type": "json",
            }
            try:
                res = requests.get(url, params=params, timeout=15)
                if not res.text.strip():
                    continue
                data = res.json()
                header = data.get("response", {}).get("header", {})
                if header.get("resultCode", "") not in ("00", "0", ""):
                    continue
                body = data.get("response", {}).get("body", {})
                items = body.get("items", {})
                if not items:
                    continue
                item_list = items.get("item", [])
                if isinstance(item_list, dict):
                    item_list = [item_list]
                if not item_list:
                    continue
                item_list.sort(key=lambda x: str(x.get("crtnDay", "")), reverse=True)
                item = item_list[0]
                return {
                    "주택유형코드": item.get("hstpGbCd", "") or "",
                    "주택유형코드명": item.get("hstpGbCdNm", "") or "",
                    "건물명": item.get("bldNm", "") or "",
                }, None, all_debug
            except (requests.exceptions.RequestException, ValueError):
                continue
    return None, "건축인허가 주택유형 정보 없음", all_debug


def normalize_hstp_type(code_nm, code=""):
    nm = str(code_nm or "").strip()
    cd = str(code or "").strip()
    if "도시형생활주택" in nm: return "도시형생활주택"
    if "아파트" in nm: return "아파트"
    if "다세대" in nm: return "다세대주택"
    if "연립" in nm: return "연립주택"
    if "단독" in nm and "다가구" in nm: return "단독(다가구)주택"
    if "단독" in nm: return "단독주택"
    if "오피스텔" in nm: return "오피스텔(업무시설)"
    if "공동주택" in nm: return "공동주택"
    if cd == "1": return "단독주택"
    if cd == "2": return "오피스텔(업무시설)"
    if cd == "3": return "공동주택"
    return nm or ""


# ============================================================
# 표제부 조회
# ============================================================
def fetch_building_type(sigunguCd, bjdongCd, bun, ji, platGbCd, juso=None,
                        target_dong="", target_ho=""):
    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]
    all_debug = []
    all_items_collected = []
    for bd in bjdong_variants:
        for pg in plat_candidates:
            items, err, dbg = call_api_all_pages(
                "getBrTitleInfo", sigunguCd, bd, pg, bun, ji,
                match_check=None, extra_params=None, collect_all=True)
            all_debug.extend(dbg)
            if items:
                all_items_collected.extend(items)
        if all_items_collected:
            break
    if not all_items_collected:
        return None, "표제부 정보 없음", all_debug

    chosen = all_items_collected[0]
    info = {
        "주용도코드": chosen.get("mainPurpsCd", "") or "",
        "주용도": chosen.get("mainPurpsCdNm", "") or "",
        "기타용도": chosen.get("etcPurps", "") or "",
        "세대수": chosen.get("hhldCnt", "") or chosen.get("hhlDcnt", "") or "",
        "호수": chosen.get("hoCnt", "") or "",
        "건물명": chosen.get("bldNm", "") or "",
        "동명칭": chosen.get("dongNm", "") or "",
        "사용승인일": chosen.get("useAprDay", "") or "",
        "연면적": chosen.get("totArea", "") or "",
        "대지면적": chosen.get("platArea", "") or "",
        "건축면적": chosen.get("archArea", "") or "",
        "지상층수": chosen.get("grndFlrCnt", "") or "",
    }
    need_fallback = (
        not info["지상층수"] or not info["세대수"] or
        not info["대지면적"] or
        str(info["대지면적"]).strip() in ("0", "0.0", "0.00")
    )
    if need_fallback:
        recap, recap_dbg = _fetch_recap_title(
            sigunguCd, bjdongCd, bun, ji, platGbCd, juso)
        all_debug.extend(recap_dbg)
        if recap:
            for k, v in recap.items():
                cur = info.get(k, "")
                if not cur or str(cur).strip() in ("0", "0.0", "0.00"):
                    info[k] = v
    return info, None, all_debug


def classify_housing_type(purps_cd="", purps_nm="", etc_purps="", bld_nm="",
                           hhld="", grnd_flr="", plat_area="", arch_area="",
                           ho_cnt="", tot_area=""):
    cd_raw = str(purps_cd or "").strip()
    cd = cd_raw.zfill(5) if cd_raw.isdigit() else cd_raw
    nm = str(purps_nm or "").strip()
    e = str(etc_purps or "").strip()
    bn = str(bld_nm or "").strip()
    if "도시형생활주택" in nm or "도시형생활주택" in e or "도시형생활주택" in bn:
        return "도시형생활주택"
    if cd == "02001": return "아파트"
    if cd == "02002": return "연립주택"
    if cd == "02003": return "다세대주택"
    if cd == "01003": return "단독(다가구)주택"
    if cd in ("01000", "01001"):
        if "다가구" in nm or "다가구" in e:
            return "단독(다가구)주택"
        return "단독주택"
    if cd == "14000":
        if "오피스텔" in nm or "오피스텔" in e or "오피스텔" in bn:
            return "오피스텔(업무시설)"
        return nm or "업무시설"
    if "아파트" in nm: return "아파트"
    if "다세대" in nm: return "다세대주택"
    if "연립" in nm: return "연립주택"
    if "다가구" in nm: return "단독(다가구)주택"
    if "단독" in nm: return "단독주택"
    if "오피스텔" in nm: return "오피스텔(업무시설)"
    if cd.startswith("02000") or "공동주택" in nm or "공동주택" in e:
        has_apt = any(k in bn for k in APARTMENT_BRAND_KEYWORDS)
        has_villa = any(k in bn for k in VILLA_LIKE_KEYWORDS)
        if has_apt and not has_villa: return "아파트"
        if has_villa and not has_apt:
            return "다세대주택" if any(k in bn for k in ("빌라", "빌")) else "연립주택"
        return "공동주택"
    if "아파트" in e: return "아파트"
    if "다세대" in e: return "다세대주택"
    if "연립" in e: return "연립주택"
    if "다가구" in e: return "단독(다가구)주택"
    if "단독" in e: return "단독주택"
    if any(k in f"{nm} {e}" for k in ("근린생활", "상가", "판매", "숙박", "공장", "업무시설")):
        if any(k in bn for k in APARTMENT_BRAND_KEYWORDS):
            return "아파트(주상복합 추정)"
        return nm or "비주거용"
    return nm or "미분류"


def _housing_specificity(t):
    if not t: return 0
    if "미분류" in t or "정보부족" in t: return 1
    if t == "공동주택": return 2
    if "추정" in t: return 5
    return 10


# ============================================================
# 프로세스
# ============================================================
def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "대지면적": "", "주택유형": "",
                "유형출처": "", "상태": "주소 없음"}, None

    target_dong, target_ho = extract_dong_ho(address)
    juso, msg, juso_debug = search_juso(address)
    if not juso:
        return {"주소": address, "전용면적": "", "대지면적": "", "주택유형": "",
                "유형출처": "", "상태": f"주소 검색 실패: {msg}"}, {"juso_debug": juso_debug}

    expected_bldNm = []
    if juso.get("bdNm"):
        expected_bldNm.append(juso["bdNm"])
    for n in extract_bldnm_from_parens(address):
        if n not in expected_bldNm:
            expected_bldNm.append(n)

    permit_info, _, _ = fetch_housing_type_from_permit(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"], juso["platGbCd"])

    type_info, _, _ = fetch_building_type(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], juso=juso,
        target_dong=target_dong, target_ho=target_ho)

    type_from_title = ""
    if type_info:
        bld_nm_for_title = (
            type_info.get("건물명", "") or
            (permit_info.get("건물명", "") if permit_info else "") or
            (juso.get("bdNm", "") if juso else "")
        )
        type_from_title = classify_housing_type(
            purps_cd=type_info.get("주용도코드", ""),
            purps_nm=type_info.get("주용도", ""),
            etc_purps=type_info.get("기타용도", ""),
            bld_nm=bld_nm_for_title,
            hhld=type_info.get("세대수", ""),
            grnd_flr=type_info.get("지상층수", ""),
            plat_area=type_info.get("대지면적", ""),
            arch_area=type_info.get("건축면적", ""),
            ho_cnt=type_info.get("호수", ""),
            tot_area=type_info.get("연면적", ""),
        )

    type_from_permit = ""
    if permit_info and permit_info.get("주택유형코드명"):
        type_from_permit = normalize_hstp_type(
            permit_info["주택유형코드명"], permit_info.get("주택유형코드", ""))

    if type_from_title in ("단독주택", "단독(다가구)주택") and type_info:
        tot = type_info.get("연면적", "")
        plat = type_info.get("대지면적", "")
        tot_str = plat_str = ""
        try:
            if tot and str(tot).strip() not in ("0", "0.0", "0.00"):
                tot_str = f"{float(str(tot).replace(',', '')):.2f}㎡ (연면적)"
        except (ValueError, TypeError):
            pass
        try:
            if plat and str(plat).strip() not in ("0", "0.0", "0.00"):
                plat_str = f"{float(str(plat).replace(',', '')):.2f}㎡"
        except (ValueError, TypeError):
            pass
        return {
            "주소": address, "전용면적": tot_str, "대지면적": plat_str,
            "주택유형": type_from_title, "유형출처": "표제부기반",
            "상태": f"단독주택 연면적 반환",
        }, {}

    area, status, errors, api_debug, matched_unit = find_dedicated_area(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], target_dong, target_ho, juso=juso,
        expected_bldNm=expected_bldNm)

    type_from_unit = ""
    if matched_unit:
        unit_nm = str(matched_unit.get("mainPurpsCdNm", "") or "").strip()
        unit_cd = str(matched_unit.get("mainPurpsCd", "") or "").strip()
        if unit_nm or unit_cd:
            bld_nm_for_unit = (
                matched_unit.get("bldNm", "") or
                (type_info.get("건물명", "") if type_info else "") or
                (permit_info.get("건물명", "") if permit_info else "") or
                (juso.get("bdNm", "") if juso else "")
            )
            type_from_unit = classify_housing_type(
                purps_cd=unit_cd, purps_nm=unit_nm,
                bld_nm=bld_nm_for_unit,
                hhld=type_info.get("세대수", "") if type_info else "",
                grnd_flr=type_info.get("지상층수", "") if type_info else "",
            )

    if type_from_unit == "다세대주택":
        if type_from_title == "도시형생활주택" or type_from_permit == "도시형생활주택":
            type_from_unit = ""

    type_candidates = [
        (type_from_unit, "전유부기반"),
        (type_from_title, "표제부기반"),
        (type_from_permit, "인허가API"),
    ]
    housing_type = ""
    housing_type_source = ""
    best_spec = 0
    for t, src in type_candidates:
        if not t:
            continue
        s = _housing_specificity(t)
        if s > best_spec:
            housing_type = t
            housing_type_source = src
            best_spec = s
    if not housing_type:
        for t, src in type_candidates:
            if t:
                housing_type = t
                housing_type_source = src
                break

    if area:
        conf = "확정" if (target_dong and target_ho) else "추정"
        return {
            "주소": address, "전용면적": f"{area:.2f}㎡", "대지면적": "",
            "주택유형": housing_type or "-", "유형출처": housing_type_source,
            "상태": f"{status} [{conf}]",
        }, {}

    if type_info and type_info.get("연면적"):
        try:
            tot_val = float(str(type_info["연면적"]).replace(",", ""))
            return {
                "주소": address,
                "전용면적": f"{tot_val:.2f}㎡ (연면적 폴백)",
                "대지면적": "",
                "주택유형": housing_type or "-",
                "유형출처": housing_type_source,
                "상태": "전유부 조회 실패, 표제부 연면적으로 대체",
            }, {}
        except (ValueError, TypeError):
            pass

    return {
        "주소": address, "전용면적": "", "대지면적": "",
        "주택유형": housing_type or "-", "유형출처": housing_type_source,
        "상태": status,
    }, {}


# ============================================================
# 백그라운드 조회
# ============================================================
def _background_query(query_id, addresses, user_id):
    status_path = os.path.join(QUERY_DIR, f"{user_id}_{query_id}_status.json")
    results_path = os.path.join(QUERY_DIR, f"{user_id}_{query_id}_results.json")
    try:
        results = []
        for i, addr in enumerate(addresses, 1):
            try:
                with open(status_path, "w", encoding="utf-8") as f:
                    json.dump({"status": "running", "current": i,
                               "total": len(addresses), "address": addr},
                              f, ensure_ascii=False)
            except IOError:
                pass
            try:
                result, _ = process_address(addr, progress=None)
            except Exception as e:
                result = {"주소": addr, "전용면적": "", "대지면적": "",
                          "주택유형": "", "유형출처": "", "상태": f"오류: {e}"}
            results.append(result)
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"status": "done", "total": len(addresses)}, f, ensure_ascii=False)
    except Exception as e:
        try:
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump({"status": "error", "error": str(e)}, f, ensure_ascii=False)
        except IOError:
            pass


# ============================================================
# 결과 렌더링
# ============================================================
def render_results(results):
    success = sum(1 for r in results
                  if r.get("전용면적") or (r.get("주택유형") and r.get("주택유형") != "-"))
    error = len(results) - success
    if error == 0:
        bg = "linear-gradient(90deg, #28a745, #34ce57)"
        msg = f"✅ {len(results)}건 조회 완료"
    elif success == 0:
        bg = "linear-gradient(90deg, #dc3545, #e4606d)"
        msg = f"⚠️ {len(results)}건 조회 오류"
    else:
        bg = "linear-gradient(90deg, #17a2b8, #48c6dc)"
        msg = f"✅ {success}건 조회 완료 | ⚠️ {error}건 조회 오류"
    st.markdown(
        f"<div style='background:{bg};color:#fff;padding:16px 22px;border-radius:10px;"
        f"font-weight:bold;font-size:1.15rem;text-align:center;'>{msg}</div>",
        unsafe_allow_html=True,
    )
    for i, r in enumerate(results, 1):
        st.markdown(f"### {i}. {r.get('주소', '')}")
        type_txt = r.get("주택유형") or "-"
        area_txt = r.get("전용면적") or "-"
        plat_txt = r.get("대지면적") or ""
        status_txt = r.get("상태", "")
        src = r.get("유형출처", "")
        badge = f" [{src}]" if src else ""
        if plat_txt:
            st.success(f"🏢 주택유형: **{type_txt}**{badge} | 📐 연면적: {area_txt} | 🗺️ 대지면적: {plat_txt} ({status_txt})")
        elif area_txt and area_txt != "-":
            st.success(f"🏢 주택유형: **{type_txt}**{badge} | 📐 면적: {area_txt} ({status_txt})")
        else:
            if type_txt and type_txt != "-":
                st.info(f"🏢 주택유형: **{type_txt}**{badge}")
            st.error(f"조회 실패: {status_txt}")
    st.markdown("---")
    st.subheader("📋 조회 결과")
    df = pd.DataFrame(results)
    for col in ["주소", "전용면적", "대지면적", "주택유형", "유형출처", "상태"]:
        if col not in df.columns:
            df[col] = ""
    df = df[["주소", "전용면적", "대지면적", "주택유형", "유형출처", "상태"]]
    st.dataframe(df, use_container_width=True, hide_index=True)
    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button("📥 CSV 다운로드", data=csv,
                       file_name="전용면적_주택유형_조회결과.csv",
                       mime="text/csv", use_container_width=True)


# ============================================================
# 상태 폴링
# ============================================================
@st.fragment(run_every=1.5)
def render_query_status():
    qid = st.session_state.get("_current_query_id")
    if not qid:
        return
    cache_key = f"_results_cache_{qid}"
    if cache_key in st.session_state:
        render_results(st.session_state[cache_key])
        return
    status_path = os.path.join(QUERY_DIR, f"{USER_ID}_{qid}_status.json")
    results_path = os.path.join(QUERY_DIR, f"{USER_ID}_{qid}_results.json")
    if not os.path.exists(status_path):
        st.info("⏳ 조회 준비 중...")
        return
    try:
        with open(status_path, encoding="utf-8") as f:
            status = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    if status["status"] == "running":
        st.markdown(
            f"""
            <style>
              @keyframes blinkPulse {{
                0%   {{ opacity: 1;   box-shadow: 0 0 12px rgba(255,140,0,0.85); }}
                50%  {{ opacity: 0.55; box-shadow: 0 0 4px rgba(255,140,0,0.3); }}
                100% {{ opacity: 1;   box-shadow: 0 0 12px rgba(255,140,0,0.85); }}
              }}
            </style>
            <div style='background:linear-gradient(90deg,#ff9a00,#ff6a00);color:#fff;
                        padding:14px 20px;border-radius:10px;font-weight:bold;
                        font-size:1.15rem;animation:blinkPulse 1.2s ease-in-out infinite;'>
              ⏳ 조회 중... {status['current']} / {status['total']} 건
              <span style='display:block;margin-top:6px;font-weight:normal;
                          font-size:0.92rem;opacity:0.95;'>📍 {status['address']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif status["status"] == "done":
        try:
            with open(results_path, encoding="utf-8") as f:
                results = json.load(f)
            st.session_state[cache_key] = results
            render_results(results)
        except (json.JSONDecodeError, IOError):
            st.error("결과 파일 읽기 실패")
    elif status["status"] == "error":
        st.error(f"조회 오류: {status.get('error', '알 수 없음')}")


# ============================================================
# 7. UI
# ============================================================
st.title("🏠 건축물 전용면적 · 주택유형 조회")

# ============================================================
# ★ 최근 조회 결과 (모든 결과 노출 / 기본 접힘 / 눈에 안 띄는 토글)
# ============================================================
if "show_recent" not in st.session_state:
    st.session_state.show_recent = False

# ★ 모든 사용자/세션의 결과 파일 (필터 없음)
recent_files = sorted(
    glob.glob(os.path.join(QUERY_DIR, "*_results.json")),
    key=os.path.getmtime, reverse=True
)[:5]

if recent_files:
    # 눈에 안 띄는 조용한 토글 (커스텀 CSS)
    st.markdown(
        """
        <style>
          div.recent-toggle .stButton > button {
            background: transparent !important;
            border: none !important;
            color: #b8b8b8 !important;
            font-size: 0.78rem !important;
            font-weight: 400 !important;
            padding: 0 !important;
            margin: 0 !important;
            min-height: 0 !important;
            height: auto !important;
            line-height: 1.3 !important;
            text-align: left !important;
            box-shadow: none !important;
            justify-content: flex-start !important;
          }
          div.recent-toggle .stButton > button:hover {
            color: #777 !important;
            background: transparent !important;
            border: none !important;
          }
          div.recent-toggle .stButton > button:focus {
            color: #777 !important;
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="recent-toggle">', unsafe_allow_html=True)
    arrow = "▾" if st.session_state.show_recent else "▸"
    if st.button(
        f"{arrow} 최근 조회 결과 {len(recent_files)}건",
        key="toggle_recent_btn",
    ):
        st.session_state.show_recent = not st.session_state.show_recent
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    # ★ 접혀있을 때는 아무것도 안 보임. 펼쳤을 때만 결과 리스트
    if st.session_state.show_recent:
        with st.container(border=True):
            st.caption("📌 최근 조회 결과입니다. (탭을 닫아도 여기 남아있음)")
            for rf in recent_files:
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(rf)).strftime("%m-%d %H:%M")
                    with open(rf, encoding="utf-8") as fh:
                        data = json.load(fh)
                    # ★ 파일명에서 "_results.json" 만 제거 (USER_ID prefix 유지)
                    file_key = os.path.basename(rf).replace("_results.json", "")
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.caption(f"[{mtime}] {len(data)}건")
                    with c2:
                        if st.button("불러오기", key=f"load_{file_key}", use_container_width=True):
                            st.session_state["_current_query_id"] = file_key
                            st.session_state[f"_results_cache_{file_key}"] = data
                            st.rerun()
                except (json.JSONDecodeError, IOError):
                    pass

# ============================================================
# 도로명주소 검색
# ============================================================
with st.expander("🔍 도로명주소 / 건물명으로 먼저 검색해보기 (주소를 모를 때)", expanded=False):
    st.caption("건물명 또는 주소 일부를 입력하세요.")
    if "search_candidates" not in st.session_state:
        st.session_state.search_candidates = []
    if "search_error" not in st.session_state:
        st.session_state.search_error = None
    if "search_performed" not in st.session_state:
        st.session_state.search_performed = False
    if "show_search_results" not in st.session_state:
        st.session_state.show_search_results = True

    col1, col2 = st.columns([4, 1])
    with col1:
        search_kw = st.text_input("검색어", key="juso_search_kw",
                                  placeholder="예: 반포자이 / 관문로 106",
                                  label_visibility="collapsed")
    with col2:
        do_search = st.button("🔍 검색", use_container_width=True)

    if do_search:
        if not search_kw.strip():
            st.warning("검색어를 입력해주세요.")
        else:
            with st.spinner("검색 중..."):
                candidates, err = search_address_candidates(search_kw.strip(), count=20)
            st.session_state.search_candidates = candidates
            st.session_state.search_error = err
            st.session_state.search_performed = True
            st.session_state.show_search_results = True

    if st.session_state.search_performed:
        err = st.session_state.search_error
        candidates = st.session_state.search_candidates
        if err:
            st.error(f"검색 실패: {err}")
        elif not candidates:
            st.info("검색 결과가 없습니다.")
        else:
            head_col1, head_col2 = st.columns([4, 1])
            with head_col1:
                st.markdown(f"### 🔍 총 {len(candidates)}건 검색됨")
            with head_col2:
                toggle_label = "🔼 결과 접기" if st.session_state.show_search_results else "🔽 결과 펼치기"
                if st.button(toggle_label, use_container_width=True, key="toggle_search_top"):
                    st.session_state.show_search_results = not st.session_state.show_search_results
                    st.rerun()
            if st.session_state.show_search_results:
                next_slot_1based = get_next_empty_slot()
                st.info(f"**⭐ 주소 {next_slot_1based} (다음 빈슬롯)** 이 기본 선택됩니다.")
                for i, c in enumerate(candidates, start=1):
                    with st.container(border=True):
                        top_left, top_right = st.columns([5, 1])
                        with top_left:
                            st.markdown(f"**{i}. {c['도로명주소']}**  `{c['우편번호']}`")
                        with top_right:
                            if st.button("🔽 접기", key=f"collapse_{i}", use_container_width=True):
                                st.session_state.show_search_results = False
                                st.rerun()
                        detail_key = f"detail_{i}"
                        with st.container(border=True):
                            st.markdown("##### 📝 상세주소 (동/호수) 입력")
                            detail_val = st.text_input("상세주소", key=detail_key,
                                                       placeholder="예: 113동 401호",
                                                       label_visibility="collapsed")
                            st.markdown(f"📄 **원본:** `{combine_address(c['도로명주소'], detail_val)}`  \n"
                                        f"📄 **괄호제거:** `{combine_address(c['도로명주소_깔끔'], detail_val)}`")
                        if c["건물명"]:
                            st.caption(f"🏢 건물명: {c['건물명']}")
                        if c["지번주소"]:
                            st.caption(f"📍 지번: {c['지번주소']}")
                        btn_a, btn_b = st.columns(2)
                        with btn_a:
                            if st.button("📥 원본 주소 넣기", key=f"orig_{i}", use_container_width=True):
                                st.session_state["_picker_open"] = {
                                    "candidate": c, "detail_key": detail_key,
                                    "base_key": "도로명주소", "next_slot": next_slot_1based,
                                }
                                st.rerun()
                        with btn_b:
                            if st.button("📥 괄호 제거 주소 넣기", key=f"clean_{i}", use_container_width=True):
                                st.session_state["_picker_open"] = {
                                    "candidate": c, "detail_key": detail_key,
                                    "base_key": "도로명주소_깔끔", "next_slot": next_slot_1based,
                                }
                                st.rerun()
            else:
                st.info("🔽 검색 결과가 접혀 있습니다.")

st.markdown("---")
_flash = st.session_state.pop("_flash_msg", None)
if _flash:
    st.success(_flash)

st.subheader("📋 전용면적 · 주택유형 조회")
st.caption("💡 'OO동 OOOO호' 형식 권장. **탭을 닫아도 서버가 살아있으면 조회가 계속됩니다.**")

addresses = []
for i in range(st.session_state.address_count):
    address = st.text_input(f"주소 {i + 1}", key=f"address_{i}",
                            placeholder="예: 서울특별시 성동구 왕십리로 410 센트라스 113동 401호")
    addresses.append(address.strip())

ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1, 1, 2])
with ctrl_col1:
    if st.button("➕ 주소 추가",
                 disabled=(st.session_state.address_count >= MAX_ADDRESS),
                 use_container_width=True, key="btn_add_addr"):
        st.session_state.address_count += 1
        st.rerun()
with ctrl_col2:
    if st.button("➖ 마지막 삭제",
                 disabled=(st.session_state.address_count <= 1),
                 use_container_width=True, key="btn_remove_addr"):
        remove_idx = st.session_state.address_count - 1
        st.session_state.pop(f"address_{remove_idx}", None)
        st.session_state.address_count -= 1
        st.rerun()
with ctrl_col3:
    do_query = st.button("🔎 조회 (백그라운드 실행)", type="primary",
                         use_container_width=True, key="btn_query_top")

st.caption(f"현재 입력칸: **{st.session_state.address_count} / {MAX_ADDRESS}** 개")
st.markdown("---")

if do_query:
    targets = [x for x in addresses if x]
    if not targets:
        st.warning("주소를 하나 이상 입력해주세요.")
        st.stop()
    query_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    st.session_state["_current_query_id"] = query_id
    thread = threading.Thread(
        target=_background_query,
        args=(query_id, targets, USER_ID),
        daemon=True,
    )
    thread.start()
    st.success(f"🚀 백그라운드 조회 시작! (ID: `{query_id}`)")
    st.info("💡 탭을 닫아도 서버가 살아있으면 조회가 계속됩니다. 다시 접속하면 상단 **최근 조회 결과**에서 확인하세요.")
    time.sleep(0.5)
    st.rerun()

render_query_status()

_picker = st.session_state.pop("_picker_open", None)
if _picker:
    slot_picker_dialog(_picker["candidate"], _picker["detail_key"],
                       _picker["base_key"], _picker["next_slot"])
