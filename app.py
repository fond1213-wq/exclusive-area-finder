import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st

# ============================================================
# 1. 기본 설정
# ============================================================
st.set_page_config(page_title="전용면적 조회", page_icon="🏠", layout="wide")

BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

# 필수 키 검증
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

DEBUG = st.sidebar.checkbox("🔧 디버그 모드", value=True)

APARTMENT_BRAND_KEYWORDS = (
    "자이", "푸르지오", "래미안", "힐스테이트", "아이파크", "e편한세상", "편한세상",
    "더샵", "롯데캐슬", "SK뷰", "한신더휴", "한신", "금호어울림", "코오롱하늘채",
    "호반베르디움", "반도유보라", "부영사랑으로", "중흥S클래스", "제일풍경채",
    "대방노블랜드", "우미린", "벽산블루밍", "동원로얄듀크", "KCC스위첸",
    "한화포레나", "포레나", "두산위브", "현대홈타운", "아파트", "APT",
)
VILLA_LIKE_KEYWORDS = ("빌라", "타운하우스", "빌", "홈타운")

# ============================================================
# 2. 파싱 유틸
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
# 3. Juso API
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
            dbg["raw_text"] = res.text[:500]
            return [], "JSON 파싱 실패", dbg
        common = data.get("results", {}).get("common", {})
        if common.get("errorCode", "") not in ("", "0"):
            return [], f"오류 [{common.get('errorCode')}]", dbg
        juso_list = data.get("results", {}).get("juso", [])
        dbg["juso_count"] = len(juso_list)
        dbg["juso_short"] = [
            {k: j.get(k, "") for k in ("roadAddr", "jibunAddr", "bdNm", "detBdNmList",
                                       "relJibun", "lnbrMnnm", "lnbrSlno", "buldMnnm", "buldSlno",
                                       "naBjdongCd", "naMainBun", "naSubBun")}
            for j in juso_list
        ]
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

# ============================================================
# 3-1. 도로명주소 검색
# ============================================================
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
# 4. 국토부 API 호출 (전유부·표제부용 — BUILDING_API_KEY)
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
                debug_snippets.append({"endpoint": endpoint, "note": "empty_after_4",
                    "params": {k: v for k, v in params.items() if k != "serviceKey"}})
                break

            try:
                data = res.json()
            except ValueError:
                error_msg = f"[{endpoint}] JSON 파싱 실패"
                debug_snippets.append({"endpoint": endpoint, "status": res.status_code,
                    "params": {k: v for k, v in params.items() if k != "serviceKey"}})
                break

            header = data.get("response", {}).get("header", {})
            if header.get("resultCode", "") not in ("00", "0", ""):
                error_msg = f"[{endpoint}] API 오류 [{header.get('resultCode')}]"
                debug_snippets.append({"endpoint": endpoint, "header": header,
                    "params": {k: v for k, v in params.items() if k != "serviceKey"}})
                break

            body = data.get("response", {}).get("body", {})
            items = body.get("items", {})
            if page == 1:
                sample = items
                if isinstance(sample, dict) and isinstance(sample.get("item"), list):
                    sample = {**sample, "item": sample["item"][:2]}
                debug_snippets.append({"endpoint": endpoint,
                    "totalCount": body.get("totalCount", 0), "sample_short": sample,
                    "params": {k: v for k, v in params.items() if k != "serviceKey"}})
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
# 5. 매칭 엔진
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


def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, platGbCd,
                        target_dong, target_ho, juso=None, expected_bldNm=None):
    if not target_ho:
        return None, "호수를 인식하지 못했습니다.", [], []

    expected_bldNm = expected_bldNm or []
    all_errors, all_debug = [], []

    def _is_exclusive(item):
        return str(item.get("exposPubuseGbCd", "")).strip() == "1" or \
               ("전유" in str(item.get("exposPubuseGbCdNm", "")) and
                "공용" not in str(item.get("exposPubuseGbCdNm", "")))

    def _safe_area(item):
        try:
            v = float(str(item.get("area", "0")).replace(",", ""))
            if 5.0 <= v <= 500.0:
                return round(v, 2)
        except (ValueError, TypeError):
            pass
        return None

    def _ho_num(s):
        nums = re.findall(r"\d+", str(s or ""))
        return nums[-1] if nums else ""

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
        ok = is_match(target_ho, h) or (_ho_num(h) == str(target_ho).strip())
        if not ok:
            return False
        if target_dong:
            d = (item.get("dongNm") or "").strip()
            if not d:
                return True
            return is_match(target_dong, d)
        return True

    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)

    bun_ji_candidates = [(bun, ji)]
    if ji != "0":
        bun_ji_candidates.append((bun, "0"))
    if juso and juso.get("buldMnnm", "").isdigit():
        bs = juso.get("buldSlno", "") if juso.get("buldSlno", "").isdigit() else "0"
        bun_ji_candidates.append((juso["buldMnnm"], bs))
        if bs != "0":
            bun_ji_candidates.append((juso["buldMnnm"], "0"))
    if juso and juso.get("relJibun"):
        for rb, rj in parse_rel_jibun(juso["relJibun"]):
            bun_ji_candidates.append((rb, rj))

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

    filter_attempts = []
    if target_dong:
        filter_attempts = [
            {"dongNm": f"{target_dong}동", "hoNm": str(target_ho)},
            {"dongNm": f"{target_dong}동", "hoNm": f"{target_ho}호"},
            {"dongNm": str(target_dong), "hoNm": str(target_ho)},
            {"dongNm": str(target_dong), "hoNm": f"{target_ho}호"},
        ]
    filter_attempts.append({"hoNm": str(target_ho)})
    filter_attempts.append({"hoNm": f"{target_ho}호"})

    for pg, bd, b, j in combos[:8]:
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
                        return val, "조회 성공 (필터)", all_errors, all_debug

    survey_items = []
    any_data_found = False
    for pg, bd, b, j in combos:
        area_list, err1, dbg1 = call_api_all_pages(
            "getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j,
            match_check=None, extra_params=None, collect_all=True)
        if err1:
            all_errors.append(err1)
        all_debug.extend(dbg1)
        if area_list:
            any_data_found = True
            survey_items.extend(area_list)
            matched = [a for a in area_list if _matches(a)]
            if matched:
                val = _safe_area(matched[0])
                if val is not None:
                    return val, "조회 성공 (전체조회)", all_errors, all_debug
            break

    if not any_data_found:
        tried = [f"{pg}/{bd}/{b}-{j}" for pg, bd, b, j in combos[:8]]
        return None, (f"전유부·표제부 모두 데이터 없음. "
                      f"입력 지번: {bun}-{ji}, bjdongCd 변형: {bjdong_variants}. "
                      f"시도한 조합(앞 8개): {tried}"), all_errors, all_debug

    all_bldnms = sorted({(i.get("bldNm") or "").strip() for i in survey_items
                         if (i.get("bldNm") or "").strip()})
    all_dongs = sorted({(i.get("dongNm") or "").strip() for i in survey_items
                        if (i.get("dongNm") or "").strip()})
    all_hos = sorted({(i.get("hoNm") or "").strip() for i in survey_items
                      if (i.get("hoNm") or "").strip()})
    similar_hos = [h for h in all_hos if _ho_num(h) == str(target_ho).strip()]

    hint_parts = []
    if expected_bldNm and all_bldnms:
        normalized_expected = {normalize_bldnm(n) for n in expected_bldNm if n}
        other_bldnms = [b for b in all_bldnms if normalize_bldnm(b) not in normalized_expected]
        if other_bldnms and len(other_bldnms) == len(all_bldnms):
            hint_parts.append(
                f"⚠️ 이 지번의 데이터는 다른 건물입니다. "
                f"검색한 건물명({', '.join(expected_bldNm)})과 일치하지 않음. "
                f"실제 존재하는 건물: {', '.join(all_bldnms[:5])}"
            )
    if all_bldnms:
        hint_parts.append(f"bldNm({len(all_bldnms)}): {', '.join(all_bldnms[:5])}")
    if all_dongs:
        hint_parts.append(f"dongNm({len(all_dongs)}): {', '.join(all_dongs[:10])}")
    if all_hos:
        preview = ", ".join(all_hos[:30])
        more = f"... (총 {len(all_hos)}개)" if len(all_hos) > 30 else ""
        hint_parts.append(f"hoNm: [{preview}]{more}")
    if similar_hos:
        hint_parts.append(f"★ '{target_ho}'와 숫자 같은 hoNm: {similar_hos}")

    hint = " | ".join(hint_parts) if hint_parts else ""
    status = f"동/호 매칭 실패 (입력: {target_dong or '?'}동 {target_ho}호)"
    if hint:
        status += f" | {hint}"
    if all_errors:
        status += f" | API 오류: {all_errors[0]}"
    return None, status, all_errors, all_debug


# ============================================================
# ★ 5-1. 건축인허가정보 API: 주택유형 조회 (PERMIT_API_KEY 사용)
# ============================================================
def fetch_housing_type_from_permit(sigunguCd, bjdongCd, bun, ji, platGbCd):
    """
    건축HUB 건축인허가정보 서비스의 getBrHstpInfo 엔드포인트 호출
    → 주택유형구분코드명(hstpGbCdNm) 반환
    ★ 전용 별도 인증키(PERMIT_API_KEY) 사용
    """
    url = f"{BUILDING_API_BASE}/getBrHstpInfo"
    bjdong_variants = _bjdong_variants(bjdongCd)
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]

    all_debug = []
    for bd in bjdong_variants:
        for pg in plat_candidates:
            params = {
                "serviceKey": PERMIT_API_KEY_DECODED,   # ★ 인허가 전용 키
                "sigunguCd": sigunguCd,
                "bjdongCd": bd,
                "platGbCd": pg,
                "bun": str(bun).zfill(4),
                "ji": str(ji).zfill(4),
                "numOfRows": "100",
                "pageNo": "1",
                "_type": "json",
            }
            try:
                res = requests.get(url, params=params, timeout=15)
                if not res.text.strip():
                    continue
                data = res.json()
                header = data.get("response", {}).get("header", {})
                if header.get("resultCode", "") not in ("00", "0", ""):
                    all_debug.append({
                        "endpoint": "getBrHstpInfo",
                        "resultCode": header.get("resultCode"),
                        "resultMsg": header.get("resultMsg"),
                        "params": {k: v for k, v in params.items() if k != "serviceKey"},
                    })
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

                item_list.sort(
                    key=lambda x: str(x.get("crtnDay", "")), reverse=True
                )
                item = item_list[0]

                info = {
                    "주택유형코드": item.get("hstpGbCd", "") or "",
                    "주택유형코드명": item.get("hstpGbCdNm", "") or "",
                    "실호세대수": item.get("silHoHhldCnt", "") or "",
                    "실호세대수면적": item.get("silHoHhldArea", "") or "",
                    "건물명": item.get("bldNm", "") or "",
                    "대지위치": item.get("platPlc", "") or "",
                    "생성일자": item.get("crtnDay", "") or "",
                    "관리허가대장PK": item.get("mgmPmsrgstPk", "") or "",
                }
                all_debug.append({
                    "endpoint": "getBrHstpInfo",
                    "params": {k: v for k, v in params.items() if k != "serviceKey"},
                    "result": info
                })
                return info, None, all_debug
            except (requests.exceptions.RequestException, ValueError) as e:
                all_debug.append({"endpoint": "getBrHstpInfo", "error": str(e)})
                continue

    return None, "건축인허가 주택유형 정보 없음", all_debug


def normalize_hstp_type(code_nm, code=""):
    """건축인허가 API의 주택유형구분코드명을 표준 명칭으로 정규화"""
    nm = str(code_nm or "").strip()
    cd = str(code or "").strip()

    if "아파트" in nm:
        return "아파트"
    if "다세대" in nm:
        return "다세대주택"
    if "연립" in nm:
        return "연립주택"
    if "단독" in nm and "다가구" in nm:
        return "단독(다가구)주택"
    if "단독" in nm:
        return "단독주택"
    if "다가구" in nm:
        return "단독(다가구)주택"
    if "오피스텔" in nm:
        return "오피스텔(업무시설)"
    if "공동주택" in nm:
        return "공동주택"

    if cd == "1":
        return "단독주택"
    if cd == "2":
        return "오피스텔(업무시설)"
    if cd == "3":
        return "공동주택"

    return nm or ""


# ============================================================
# ★ 5-2. 표제부 조회 (BUILDING_API_KEY 사용)
# ============================================================
def fetch_building_type(sigunguCd, bjdongCd, bun, ji, platGbCd, juso=None):
    """표제부(getBrTitleInfo)에서 주용도/기타용도/세대수/면적 등 조회"""
    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    plat_candidates = [platGbCd] + [p for p in ("0", "1", "2") if p != platGbCd]

    all_debug = []
    for bd in bjdong_variants:
        for pg in plat_candidates:
            items, err, dbg = call_api_all_pages(
                "getBrTitleInfo", sigunguCd, bd, pg, bun, ji,
                match_check=None, extra_params=None, collect_all=True)
            all_debug.extend(dbg)
            if items:
                items_sorted = sorted(items, key=lambda x: str(x.get("useAprDay", "")), reverse=True)
                item = items_sorted[0]
                info = {
                    "주용도코드": item.get("mainPurpsCd", "") or "",
                    "주용도": item.get("mainPurpsCdNm", "") or "",
                    "기타용도": item.get("etcPurps", "") or "",
                    "세대수": item.get("hhldCnt", "") or "",
                    "호수": item.get("hoCnt", "") or "",
                    "가구수": item.get("fmlyCnt", "") or "",
                    "건물명": item.get("bldNm", "") or "",
                    "사용승인일": item.get("useAprDay", "") or "",
                    "연면적": item.get("totArea", "") or "",
                    "대지면적": item.get("platArea", "") or "",
                    "건축면적": item.get("archArea", "") or "",
                    "지상층수": item.get("grndFlrCnt", "") or "",
                    "지하층수": item.get("ugrFlrCnt", "") or "",
                    "동명칭": item.get("dongNm", "") or "",
                }
                return info, None, all_debug
    return None, "표제부 정보 없음", all_debug


def classify_housing_type(purps_cd="", purps_nm="", etc_purps="", bld_nm="",
                           hhld="", grnd_flr="", plat_area="", arch_area="",
                           ho_cnt="", tot_area=""):
    """주용도코드 + 주용도명 + 건물명 + 층수 + 면적 + 세대수 조합 정밀 판별"""

    def _to_int(v):
        try:
            return int(float(str(v).replace(",", "").strip()))
        except (ValueError, TypeError, AttributeError):
            return None

    def _to_float(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (ValueError, TypeError, AttributeError):
            return None

    cd_raw = str(purps_cd or "").strip()
    try:
        cd = cd_raw.zfill(5) if cd_raw.isdigit() else cd_raw
    except (ValueError, AttributeError):
        cd = cd_raw

    nm = str(purps_nm or "").strip()
    e = str(etc_purps or "").strip()
    bn = str(bld_nm or "").strip()
    combined = f"{nm} {e}"

    hhld_n = _to_int(hhld)
    ho_n = _to_int(ho_cnt)
    flr_n = _to_int(grnd_flr)
    plat_n = _to_float(plat_area)
    arch_n = _to_float(arch_area)
    tot_n = _to_float(tot_area)

    has_villa_kw = any(k in bn for k in VILLA_LIKE_KEYWORDS)
    has_apt_kw = any(k in bn for k in APARTMENT_BRAND_KEYWORDS)

    # ---------- 1) 주용도코드 최우선 매칭 ----------
    if cd == "02001":
        return "아파트"
    if cd == "02002":
        return "연립주택"
    if cd == "02003":
        return "다세대주택"
    if cd == "01003":
        return "단독(다가구)주택"
    if cd in ("01000", "01001"):
        if hhld_n is not None and hhld_n >= 2:
            return "단독(다가구)주택"
        if "다가구" in nm or "다가구" in e:
            return "단독(다가구)주택"
        return "단독주택"
    if cd == "14000":
        if "오피스텔" in nm or "오피스텔" in e or "오피스텔" in bn:
            return "오피스텔(업무시설)"
        return nm or "업무시설"

    # ---------- 2) 주용도명 키워드 ----------
    if "아파트" in nm:
        return "아파트"
    if "다세대" in nm:
        return "다세대주택"
    if "연립" in nm:
        return "연립주택"
    if "다가구" in nm:
        return "단독(다가구)주택"
    if "오피스텔" in nm or "오피스텔" in e or "오피스텔" in bn:
        return "오피스텔(업무시설)"

    # ---------- 3) 코드 02000 (공동주택 통합) 정밀 판별 ----------
    if cd.startswith("02000") or "공동주택" in nm or "공동주택" in e:
        if has_villa_kw and not has_apt_kw:
            if flr_n is not None and flr_n <= 4:
                area_ref = arch_n if arch_n is not None else plat_n
                if area_ref is not None:
                    return "연립주택" if area_ref > 660 else "다세대주택"
                return "다세대주택"
            if flr_n is not None and flr_n >= 5:
                return "다세대주택(5층↑)"
            return "다세대주택"
        if has_apt_kw:
            return "아파트"
        if flr_n is not None:
            if flr_n >= 5:
                if hhld_n is not None and hhld_n < 30:
                    return "다세대주택(5층↑·소규모)"
                return "아파트"
            else:
                area_ref = arch_n if arch_n is not None else plat_n
                if area_ref is not None:
                    return "연립주택" if area_ref > 660 else "다세대주택"
                if hhld_n is not None:
                    return "다세대주택" if hhld_n <= 4 else "연립주택"
                return "다세대주택(추정)"
        if hhld_n is not None:
            if hhld_n >= 50:
                return "아파트"
            if hhld_n >= 5:
                return "연립주택(추정)"
            return "다세대주택(추정)"
        if has_villa_kw:
            return "다세대주택(추정)"
        if has_apt_kw:
            return "아파트(추정)"
        return "공동주택(정보부족)"

    # ---------- 4) 기타용도 ----------
    if "아파트" in e: return "아파트"
    if "다세대" in e: return "다세대주택"
    if "연립" in e: return "연립주택"
    if "다가구" in e: return "단독(다가구)주택"
    if "단독" in e: return "단독주택"
    if "오피스텔" in e: return "오피스텔(업무시설)"

    if any(k in combined for k in ("근린생활", "상가", "판매", "숙박", "공장")):
        return nm or "비주거용"

    return nm or "미분류"


# ============================================================
# 6. 프로세스 실행
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

    if progress:
        progress.write(
            f"① 매칭({juso.get('_matched_by', '-')}): {juso['jibunAddr']} "
            f"| 법정동: {juso['sigunguCd']}{juso['bjdongCd']} (alt:{juso.get('bjdongCd_alt') or '-'}) "
            f"| 지번: {juso['bun']}-{juso['ji']} "
            f"| 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}' "
            f"| 건물명: {expected_bldNm or '없음'}"
        )
        if juso.get("relJibun"):
            progress.caption(f"관련지번: {juso['relJibun']}")

    # ★ (1) 건축인허가정보 API에서 주택유형 조회 (PERMIT_API_KEY)
    permit_info, permit_err, permit_dbg = fetch_housing_type_from_permit(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"])

    # ★ (2) 표제부 조회 → 주택유형 보조 + 단독주택 면적 (BUILDING_API_KEY)
    type_info, type_err, type_dbg = fetch_building_type(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], juso=juso)

    # ★ (3) 주택유형 판별
    housing_type = ""
    housing_type_source = ""

    if permit_info and permit_info.get("주택유형코드명"):
        housing_type = normalize_hstp_type(
            permit_info["주택유형코드명"],
            permit_info.get("주택유형코드", "")
        )
        housing_type_source = "인허가API"

    # 인허가 API가 "공동주택"이거나 결과 없음 → 표제부 기반 세분화
    if housing_type in ("공동주택", "") and type_info:
        housing_type = classify_housing_type(
            purps_cd=type_info.get("주용도코드", ""),
            purps_nm=type_info.get("주용도", ""),
            etc_purps=type_info.get("기타용도", ""),
            bld_nm=type_info.get("건물명", "") or (
                permit_info.get("건물명", "") if permit_info else ""),
            hhld=type_info.get("세대수", ""),
            grnd_flr=type_info.get("지상층수", ""),
            plat_area=type_info.get("대지면적", ""),
            arch_area=type_info.get("건축면적", ""),
            ho_cnt=type_info.get("호수", ""),
            tot_area=type_info.get("연면적", ""),
        )
        housing_type_source = "표제부기반"

    # ★ (4) 단독주택이면 표제부 연면적 + 대지면적 반환
    is_single_house = housing_type in ("단독주택", "단독(다가구)주택")
    if is_single_house and type_info:
        tot = type_info.get("연면적", "")
        plat = type_info.get("대지면적", "")

        tot_str = ""
        plat_str = ""
        try:
            if tot:
                tot_str = f"{float(str(tot).replace(',', '')):.2f}㎡ (연면적)"
        except (ValueError, TypeError):
            pass
        try:
            if plat:
                plat_str = f"{float(str(plat).replace(',', '')):.2f}㎡"
        except (ValueError, TypeError):
            pass

        debug_bundle = {
            "juso_debug": juso_debug,
            "permit_debug": permit_dbg,
            "permit_info": permit_info,
            "type_debug": type_dbg,
            "type_info": type_info,
            "housing_type_source": housing_type_source,
        }
        return {
            "주소": address,
            "전용면적": tot_str,
            "대지면적": plat_str,
            "주택유형": housing_type,
            "유형출처": housing_type_source,
            "상태": f"단독주택 연면적 반환 (사용승인일: {type_info.get('사용승인일', '-')})",
        }, debug_bundle

    # ★ (5) 공동주택이면 전유부에서 호별 전용면적 조회
    area, status, errors, api_debug = find_dedicated_area(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], target_dong, target_ho, juso=juso,
        expected_bldNm=expected_bldNm)

    debug_bundle = {
        "juso_debug": juso_debug,
        "permit_debug": permit_dbg,
        "permit_info": permit_info,
        "api_debug": api_debug,
        "type_debug": type_dbg,
        "type_info": type_info,
        "errors": errors,
        "type_err": type_err,
        "permit_err": permit_err,
        "housing_type_source": housing_type_source,
    }

    if area:
        conf = "확정" if (target_dong and target_ho) else "추정"
        return {
            "주소": address,
            "전용면적": f"{area:.2f}㎡",
            "대지면적": "",
            "주택유형": housing_type or "-",
            "유형출처": housing_type_source,
            "상태": f"{status} [{conf}]",
        }, debug_bundle

    # 폴백: 표제부 연면적
    if type_info and type_info.get("연면적"):
        try:
            tot_val = float(str(type_info["연면적"]).replace(",", ""))
            plat_val = type_info.get("대지면적", "")
            plat_str = ""
            try:
                if plat_val:
                    plat_str = f"{float(str(plat_val).replace(',', '')):.2f}㎡"
            except (ValueError, TypeError):
                pass
            return {
                "주소": address,
                "전용면적": f"{tot_val:.2f}㎡ (연면적 폴백)",
                "대지면적": plat_str,
                "주택유형": housing_type or "-",
                "유형출처": housing_type_source,
                "상태": f"전유부 조회 실패, 표제부 연면적으로 대체 "
                        f"(건물명: {type_info.get('건물명', '-')})",
            }, debug_bundle
        except (ValueError, TypeError):
            pass

    return {
        "주소": address,
        "전용면적": "",
        "대지면적": "",
        "주택유형": housing_type or "-",
        "유형출처": housing_type_source,
        "상태": status,
    }, debug_bundle


# ============================================================
# 7. UI
# ============================================================
st.title("🏠 건축물 전용면적 · 주택유형 조회")

with st.expander("🔍 도로명주소 / 건물명으로 먼저 검색해보기 (주소를 모를 때)", expanded=False):
    st.caption("건물명(예: 과천푸르지오써밋) 또는 주소 일부(예: 관문로 106)를 입력하세요.")

    if "search_candidates" not in st.session_state:
        st.session_state.search_candidates = []
    if "search_error" not in st.session_state:
        st.session_state.search_error = None
    if "search_performed" not in st.session_state:
        st.session_state.search_performed = False

    col1, col2 = st.columns([4, 1])
    with col1:
        search_kw = st.text_input(
            "검색어", key="juso_search_kw",
            placeholder="예: 반포자이/ 관문로 106 / 신반포로33길",
            label_visibility="collapsed"
        )
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

    if st.session_state.search_performed:
        err = st.session_state.search_error
        candidates = st.session_state.search_candidates

        if err:
            st.error(f"검색 실패: {err}")
        elif not candidates:
            st.info("검색 결과가 없습니다.")
        else:
            st.success(f"총 {len(candidates)}건 검색됨")

            target_slot = st.selectbox(
                "⬆️ 이 주소를 넣을 위치",
                options=list(range(1, 11)),
                format_func=lambda x: f"주소 {x}",
                key="target_slot"
            )

            for i, c in enumerate(candidates, start=1):
                with st.container(border=True):
                    st.markdown(f"**{i}. {c['도로명주소']}**  `{c['우편번호']}`")
                    if c["건물명"]:
                        st.caption(f"🏢 건물명: {c['건물명']}")
                    if c["지번주소"]:
                        st.caption(f"📍 지번: {c['지번주소']}")
                    if c["동목록"]:
                        dong_preview = c["동목록"]
                        if len(dong_preview) > 200:
                            dong_preview = dong_preview[:200] + "..."
                        st.caption(f"🏠 동목록: {dong_preview}")

                    bcol1, bcol2 = st.columns(2)
                    with bcol1:
                        if st.button(f"⬆️ 주소 {target_slot}에 넣기 (원본)",
                                     key=f"use_full_{i}", use_container_width=True):
                            st.session_state[f"address_{target_slot - 1}"] = c["도로명주소"]
                            st.session_state["_flash_msg"] = (
                                f"✅ 주소 {target_slot}에 입력 완료: {c['도로명주소']}"
                            )
                            st.rerun()
                    with bcol2:
                        if st.button(f"⬆️ 주소 {target_slot}에 넣기 (괄호 제거)",
                                     key=f"use_clean_{i}", use_container_width=True):
                            st.session_state[f"address_{target_slot - 1}"] = c["도로명주소_깔끔"]
                            st.session_state["_flash_msg"] = (
                                f"✅ 주소 {target_slot}에 입력 완료: {c['도로명주소_깔끔']}"
                            )
                            st.rerun()

st.markdown("---")

_flash = st.session_state.pop("_flash_msg", None)
if _flash:
    st.success(_flash)

st.subheader("📋 전용면적 · 주택유형 조회")
st.caption("💡 **'OO동 OOOO호'** 형식 권장. "
           "주택유형은 **건축인허가정보 API(1순위, PERMIT_API_KEY) + 건축물대장 표제부(2순위, BUILDING_API_KEY)** 를 조합해 판별합니다. "
           "단독주택은 표제부 **연면적과 대지면적**을 함께 반환합니다.")

addresses = []
for i in range(10):
    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="예: 경기도 과천시 관문로 106 과천푸르지오써밋 OOO동 OOO호"
    )
    addresses.append(address.strip())

if st.button("🔎 조회", type="primary", use_container_width=True):
    targets = [x for x in addresses if x]
    if not targets:
        st.warning("주소를 하나 이상 입력해주세요.")
        st.stop()

    st.markdown("---")
    results = []
    for idx, address in enumerate(targets, start=1):
        st.markdown(f"### {idx}. {address}")
        pb = st.empty()
        result, dbg_bundle = process_address(address, pb)
        results.append(result)

        type_txt = result.get("주택유형") or "-"
        area_txt = result.get("전용면적") or "-"
        plat_txt = result.get("대지면적") or ""
        status_txt = result.get("상태", "")
        source_txt = result.get("유형출처", "")
        source_badge = f" [{source_txt}]" if source_txt else ""

        if plat_txt:
            (st.success if "[확정]" in status_txt else st.warning)(
                f"🏢 주택유형: **{type_txt}**{source_badge}  |  "
                f"📐 연면적: {area_txt}  |  "
                f"🗺️ 대지면적: {plat_txt}  "
                f"({status_txt})")
        elif result["전용면적"]:
            (st.success if "[확정]" in status_txt else st.warning)(
                f"🏢 주택유형: **{type_txt}**{source_badge}  |  "
                f"📐 면적: {area_txt} "
                f"({status_txt})")
        else:
            if type_txt and type_txt != "-":
                st.info(f"🏢 주택유형: **{type_txt}**{source_badge}")
            if "호별 전유면적이" in status_txt:
                st.info(f"ℹ️ {status_txt}")
            elif "다른 건물" in status_txt:
                st.warning(f"⚠️ {status_txt}")
            else:
                st.error(f"조회 실패: {status_txt}")

        if DEBUG and dbg_bundle:
            with st.expander("🔧 디버그 정보"):
                st.json(dbg_bundle)
        time.sleep(0.5)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    df = pd.DataFrame(results)
    for col in ["주소", "전용면적", "대지면적", "주택유형", "유형출처", "상태"]:
        if col not in df.columns:
            df[col] = ""
    df = df[["주소", "전용면적", "대지면적", "주택유형", "유형출처", "상태"]]
    st.dataframe(df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "📥 CSV 다운로드",
        data=csv,
        file_name="전용면적_주택유형_조회결과.csv",
        mime="text/csv",
        use_container_width=True
    )
