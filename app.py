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

if "BUILDING_API_KEY" not in st.secrets or "JUSO_API_KEY" not in st.secrets:
    st.error("⚠️ Streamlit Secrets 설정이 필요합니다.")
    st.stop()

BUILDING_API_KEY_RAW = st.secrets["BUILDING_API_KEY"]
BUILDING_API_KEY_DECODED = urllib.parse.unquote(BUILDING_API_KEY_RAW)
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

DEBUG = st.sidebar.checkbox("🔧 디버그 모드", value=True)

# ============================================================
# 2. 파싱 유틸
# ============================================================
def clean_address_for_parsing(address):
    """괄호 안 추가정보 제거"""
    text = str(address).strip()
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\s*\([^()]*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def extract_bldnm_from_parens(address):
    """괄호 안에서 건물명 후보 추출 (지명 접미어 제외)"""
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
# 4. 국토부 API 호출
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
# 5. 매칭 엔진 (bjdongCd variants + bldNm 검증)
# ============================================================
def _bjdong_variants(bjdongCd, alt=None):
    """법정동코드 변형 생성: 원본, alt, 00↔01 swap"""
    variants = []
    def _add(v):
        if v and v not in variants:
            variants.append(v)
    _add(bjdongCd)
    if alt:
        _add(alt)
    # 끝 2자리가 00이면 01로, 01이면 00으로
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

    # ---------------- 법정동코드 조합 ----------------
    bjdong_variants = _bjdong_variants(bjdongCd, juso.get("bjdongCd_alt") if juso else None)

    # ---------------- 지번 후보 ----------------
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

    # 중복 제거
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

    # ★ bjdongCd 변형을 최우선으로 배치 ★
    for bd in bjdong_variants:
        for b, j in bun_ji_candidates:
            _add(platGbCd, bd, b, j)
    # 나머지 platGbCd
    for pg in plat_candidates:
        if pg == platGbCd:
            continue
        for bd in bjdong_variants:
            for b, j in bun_ji_candidates:
                _add(pg, bd, b, j)

    # 필터 variants
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

    # ---------- 1단계: 필터 (앞 8개 combo) ----------
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

    # ---------- 2단계: 전체조회 ----------
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
            # 첫 데이터 발견하면 그 combo까지만
            break

    # ---------- 3단계: 표제부 폴백 ----------
    if not any_data_found:
        title = None
        title_dbg = []
        for bd in bjdong_variants:
            for pg in plat_candidates:
                items, terr, tdbg = call_api_all_pages(
                    "getBrTitleInfo", sigunguCd, bd, pg, bun, ji,
                    match_check=None, extra_params=None, collect_all=True)
                title_dbg.extend(tdbg)
                if items:
                    items_sorted = sorted(items, key=lambda x: str(x.get("useAprDay", "")), reverse=True)
                    title = items_sorted[0]
                    break
            if title:
                break
        all_debug.extend(title_dbg)

        if title:
            tot = title.get("totArea", "")
            hhld = title.get("hhldCnt", "")
            ho_cnt = title.get("hoCnt", "")
            purps = title.get("mainPurpsCdNm", "")
            bld_nm = title.get("bldNm", "")
            use_day = title.get("useAprDay", "")

            msg = (
                f"이 건물은 '{purps}'으로 호별 전유면적이 대장에 없습니다. "
                f"(건물명: {bld_nm or '-'}, 사용승인일: {use_day or '-'}) "
                f"연면적 {tot}㎡, 세대수 {hhld}, 호수 {ho_cnt}"
            )
            try:
                cnt = int(hhld or ho_cnt or 1)
                if cnt > 0:
                    est = float(str(tot).replace(",", "")) / cnt
                    msg += f" → 세대당 추정 약 {est:.1f}㎡"
            except (ValueError, ZeroDivisionError):
                pass
            return None, msg, all_errors, all_debug

        tried = [f"{pg}/{bd}/{b}-{j}" for pg, bd, b, j in combos[:8]]
        return None, (f"전유부·표제부 모두 데이터 없음. "
                      f"입력 지번: {bun}-{ji}, bjdongCd 변형: {bjdong_variants}. "
                      f"시도한 조합(앞 8개): {tried}"), all_errors, all_debug

    # ---------- 4단계: 매칭 실패 리포트 ----------
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
# 6. 프로세스 실행
# ============================================================
def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}, None

    target_dong, target_ho = extract_dong_ho(address)
    juso, msg, juso_debug = search_juso(address)
    if not juso:
        return {"주소": address, "전용면적": "", "상태": f"주소 검색 실패: {msg}"}, {"juso_debug": juso_debug}

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

    area, status, errors, api_debug = find_dedicated_area(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], target_dong, target_ho, juso=juso,
        expected_bldNm=expected_bldNm)

    debug_bundle = {"juso_debug": juso_debug, "api_debug": api_debug, "errors": errors}
    if area:
        conf = "확정" if (target_dong and target_ho) else "추정"
        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": f"{status} [{conf}]"}, debug_bundle
    return {"주소": address, "전용면적": "", "상태": status}, debug_bundle

# ============================================================
# 7. UI
# ============================================================
st.title("🏠 건축물 전용면적 조회")

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

st.subheader("📋 전용면적 조회")
st.caption("💡 **'OO동 OOOO호'** 형식 권장. "
           "괄호 안 추가정보(예: (중앙동, 과천 푸르지오 써밋)는 자동으로 무시됩니다.")

addresses = []
for i in range(10):
    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="예: 경기도 과천시 관문로 106 과천푸르지오써밋 OOO동 OOO호"
    )
    addresses.append(address.strip())

if st.button("🔎 전용면적 조회", type="primary", use_container_width=True):
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
        if result["전용면적"]:
            (st.success if "[확정]" in result["상태"] else st.warning)(
                f"전용면적{'' if '[확정]' in result['상태'] else '(추정)'}: "
                f"{result['전용면적']} ({result['상태']})")
        else:
            if "호별 전유면적이" in result["상태"]:
                st.info(f"ℹ️ {result['상태']}")
            elif "다른 건물" in result["상태"]:
                st.warning(f"⚠️ {result['상태']}")
            else:
                st.error(f"조회 실패: {result['상태']}")

        if DEBUG and dbg_bundle:
            with st.expander("🔧 디버그 정보"):
                st.json(dbg_bundle)
        time.sleep(0.5)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    st.dataframe(results, use_container_width=True, hide_index=True)
    csv = pd.DataFrame(results).to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "📥 CSV 다운로드",
        data=csv,
        file_name="전용면적_조회결과.csv",
        mime="text/csv",
        use_container_width=True
    )
