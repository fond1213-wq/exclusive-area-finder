import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st

# ============================================================
# 1. 기본 설정 및 API Key
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
# 2. 정규화 / 파싱
# ============================================================

def normalize_name(s):
    if s is None:
        return ""
    s = str(s).strip()
    s = re.sub(r"[동호제\s]", "", s)
    return s

def is_match(target, source):
    t = normalize_name(target)
    s = normalize_name(source)
    if not t or not s:
        return False
    if t.isdigit() and s.isdigit():
        return int(t) == int(s)
    return t.upper() == s.upper()

def extract_dong_ho(address):
    text = str(address).strip()
    dong, ho = "", ""
    m_dong = re.search(r"([0-9A-Za-z]+)\s*동(?![가-힣])", text)
    if m_dong:
        dong = m_dong.group(1)
    m_ho = re.search(r"(\d+)\s*호(?![가-힣])", text)
    if m_ho:
        ho = m_ho.group(1)
    if not dong and not ho:
        m_dash = re.search(r"(\d{2,4})-(\d{3,4})\b", text)
        if m_dash:
            dong, ho = m_dash.group(1), m_dash.group(2)
    if not ho:
        tokens = text.split()
        if tokens and tokens[-1].isdigit() and len(tokens[-1]) >= 3:
            ho = tokens[-1]
    return dong, ho

def extract_road_pattern(address):
    text = str(address).strip()
    m = re.search(r"([가-힣]+(?:로|길|대로)(?:\d+(?:로|길))?)\s*(\d+(?:-\d+)?)", text)
    if m:
        return m.group(1), m.group(2)
    return None, None

def sanitize_road_address(address):
    text = str(address).strip()
    text = re.sub(r"\s+[0-9A-Za-z]+\s*동(?![가-힣]).*", "", text)
    text = re.sub(r"\s+\d+\s*호(?![가-힣]).*", "", text)
    road, num = extract_road_pattern(text)
    if road and num:
        m = re.search(re.escape(road) + r"\s*" + re.escape(num), text)
        if m:
            return text[:m.end()].strip()
    return text.strip()

def validate_juso_match(user_input, juso_item):
    user_road, user_num = extract_road_pattern(user_input)
    if not user_road or not user_num:
        return True
    juso_addr = (juso_item.get("roadAddrPart1", "") or "") + " " + (juso_item.get("roadAddrPart2", "") or "")
    juso_norm = juso_addr.replace(" ", "")
    if user_road.replace(" ", "") not in juso_norm:
        return False
    if user_num not in juso_norm:
        return False
    return True

# ============================================================
# 3. Juso API
# ============================================================

def search_juso_single(keyword):
    params = {
        "confmKey": JUSO_API_KEY, "currentPage": 1, "countPerPage": 10,
        "keyword": keyword, "resultType": "json", "addInfoYn": "Y",
    }
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
        err_code = common.get("errorCode", "")
        if err_code and err_code != "0":
            return [], f"오류 [{err_code}] {common.get('errorMessage','')}", dbg
        juso_list = data.get("results", {}).get("juso", [])
        dbg["juso_count"] = len(juso_list)
        dbg["juso_short"] = [
            {"road": j.get("roadAddr", ""), "jibun": j.get("jibunAddr", ""),
             "bdNm": j.get("bdNm", ""), "detBdNmList": j.get("detBdNmList", "")}
            for j in juso_list
        ]
        return juso_list, "정상", dbg
    except requests.exceptions.RequestException as e:
        return [], f"요청 실패: {e}", dbg

def _juso_item_to_dict(j):
    admCd = j.get("admCd", "")
    if len(admCd) < 10:
        return None
    lnbr_mnnm = j.get("lnbrMnnm", "")
    lnbr_slno = j.get("lnbrSlno", "")
    mt_yn = j.get("mtYn", "0")
    bun = str(int(lnbr_mnnm)) if lnbr_mnnm and lnbr_mnnm.isdigit() else "0"
    ji = str(int(lnbr_slno)) if lnbr_slno and lnbr_slno.isdigit() else "0"
    plat_gb_cd = "1" if str(mt_yn) == "1" else "0"

    # ★ 단일동 판단: detBdNmList가 비어있으면 한동짜리 아파트로 간주 ★
    det_bd_list = (j.get("detBdNmList") or "").strip()
    is_single_building = (det_bd_list == "")

    return {
        "sigunguCd": admCd[:5], "bjdongCd": admCd[5:10],
        "bun": bun, "ji": ji, "platGbCd": plat_gb_cd,
        "jibunAddr": j.get("jibunAddr", ""), "bdNm": j.get("bdNm", ""),
        "roadAddr": j.get("roadAddr", ""),
        "is_single_building": is_single_building,
    }

def search_juso(address):
    all_dbg = {}
    candidates = [("raw", address)]
    clean = sanitize_road_address(address)
    if clean and clean != address:
        candidates.append(("sanitized", clean))

    last_msg = ""
    for label, kw in candidates:
        juso_list, msg, dbg = search_juso_single(kw)
        all_dbg[label] = dbg
        last_msg = msg
        if not juso_list:
            continue
        for j in juso_list:
            if not validate_juso_match(address, j):
                continue
            d = _juso_item_to_dict(j)
            if d:
                d["_matched_by"] = label
                return d, "정상", all_dbg
    return None, f"주소 매칭 실패 (검증 통과 결과 없음): {last_msg}", all_dbg

# ============================================================
# 4. 국토부 API 호출 (빈 응답 재시도 강화 + 조기 중단)
# ============================================================

def call_api_all_pages(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji,
                       match_check=None, extra_params=None, collect_all=False):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    bun_str = str(bun).zfill(4)
    ji_str = str(ji).zfill(4)

    all_items, page, error_msg, debug_snippets = [], 1, None, []
    ROWS_PER_PAGE, MAX_PAGES = 1000, 40
    total_pages_needed = None
    hit_rate_limit = False

    while True:
        params = {
            "serviceKey": BUILDING_API_KEY_DECODED,
            "sigunguCd": sigunguCd, "bjdongCd": bjdongCd,
            "platGbCd": platGbCd, "bun": bun_str, "ji": ji_str,
            "numOfRows": str(ROWS_PER_PAGE), "pageNo": str(page), "_type": "json",
        }
        if extra_params:
            params.update(extra_params)

        try:
            # ★ 빈 응답 시 최대 3회 재시도 (backoff 증가) ★
            raw_text = ""
            for attempt in range(3):
                try:
                    res = requests.get(url, params=params, timeout=20)
                    raw_text = res.text or ""
                    if raw_text.strip():
                        break
                except requests.exceptions.RequestException:
                    raw_text = ""
                time.sleep(1.0 + attempt * 1.5)

            if not raw_text.strip():
                hit_rate_limit = True
                error_msg = f"[{endpoint}] 빈 응답 3회 연속 (throttling 의심)"
                debug_snippets.append({
                    "endpoint": endpoint, "note": "empty_response_after_3_retries",
                    "params": {k: v for k, v in params.items() if k != "serviceKey"},
                })
                break

            try:
                data = res.json()
            except ValueError:
                error_msg = f"[{endpoint}] JSON 파싱 실패"
                debug_snippets.append({
                    "endpoint": endpoint, "status": res.status_code,
                    "raw": raw_text[:200],
                    "params": {k: v for k, v in params.items() if k != "serviceKey"},
                })
                break

            header = data.get("response", {}).get("header", {})
            result_code = header.get("resultCode", "")
            if result_code not in ("00", "0", ""):
                error_msg = f"[{endpoint}] API 오류 [{result_code}] {header.get('resultMsg','')}"
                debug_snippets.append({"endpoint": endpoint, "header": header,
                    "params": {k: v for k, v in params.items() if k != "serviceKey"}})
                break

            body = data.get("response", {}).get("body", {})
            items = body.get("items", {})

            if page == 1:
                sample = items
                if isinstance(sample, dict) and isinstance(sample.get("item"), list):
                    sample = {**sample, "item": sample["item"][:2]}
                debug_snippets.append({
                    "endpoint": endpoint,
                    "totalCount": body.get("totalCount", 0),
                    "sample_short": sample,
                    "params": {k: v for k, v in params.items() if k != "serviceKey"},
                })

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
            time.sleep(0.3)  # rate limit 회피용 간격 증가

        except requests.exceptions.RequestException as e:
            error_msg = f"[{endpoint}] 요청 실패: {e}"
            break

    return all_items, error_msg, debug_snippets

# ============================================================
# 5. 매칭 엔진 (단일동 대응 + 호출 최소화)
# ============================================================

def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, plat_gb_cd_hint,
                        target_dong, target_ho, is_single_building=False):
    if not target_ho:
        return None, "호수를 인식하지 못했습니다. '101동 2103호' 형식으로 입력해주세요.", [], []

    all_errors, all_debug = [], []

    def _is_exclusive(item):
        gb_cd = str(item.get("exposPubuseGbCd", "")).strip()
        gb_nm = str(item.get("exposPubuseGbCdNm", "")).strip()
        return gb_cd == "1" or ("전유" in gb_nm and "공용" not in gb_nm)

    def _safe_area(item):
        try:
            v = float(str(item.get("area", "0")).replace(",", ""))
            if 5.0 <= v <= 500.0:
                return round(v, 2)
        except (ValueError, TypeError):
            pass
        return None

    def _matches(item):
        if not _is_exclusive(item):
            return False
        d = (item.get("dongNm") or "").strip()
        h = (item.get("hoNm") or "").strip()
        if not is_match(target_ho, h):
            return False
        if target_dong:
            # ★ 데이터에 동 정보가 없으면(=단일동) 통과 ★
            if not d:
                return True
            return is_match(target_dong, d)
        return True

    # ---- 필터 세트: 단일동이면 dongNm 필터 제외 ----
    ho_only = {"hoNm": str(target_ho)}
    filter_sets = []
    if target_dong and not is_single_building:
        filter_sets.append({"dongNm": f"{target_dong}동", "hoNm": str(target_ho)})
        filter_sets.append({"dongNm": str(target_dong), "hoNm": str(target_ho)})
    # ★ 항상 hoNm-only를 폴백으로 (그리고 단일동일 땐 최우선) ★
    filter_sets.append(ho_only)

    # ---- 조회 조합: 최대한 적게 (throttling 회피) ----
    combos = [(plat_gb_cd_hint, ji)]
    if ji != "0":
        combos.append((plat_gb_cd_hint, "0"))
    if plat_gb_cd_hint != "0":
        combos.append(("0", ji))
    # 중복 제거
    seen = set()
    combos = [c for c in combos if not (c in seen or seen.add(c))]

    # ---------- 1단계: 필터 조회 ----------
    for extra in filter_sets:
        for platGbCd, current_ji in combos:
            area_list, err1, dbg1 = call_api_all_pages(
                "getBrExposPubuseAreaInfo",
                sigunguCd, bjdongCd, platGbCd, bun, current_ji,
                match_check=_matches, extra_params=extra,
            )
            if err1:
                all_errors.append(err1)
            all_debug.extend(dbg1)
            for a in area_list:
                if _matches(a):
                    val = _safe_area(a)
                    if val is not None:
                        return val, "조회 성공 (필터)", all_errors, all_debug

    # ---------- 2단계: 전체조회 (필터 없이) → 클라이언트 매칭 ----------
    #  가장 신뢰도 높은 조합부터
    survey_items = []
    for platGbCd, current_ji in combos:
        area_list, err1, dbg1 = call_api_all_pages(
            "getBrExposPubuseAreaInfo",
            sigunguCd, bjdongCd, platGbCd, bun, current_ji,
            match_check=None, extra_params=None, collect_all=True,
        )
        if err1:
            all_errors.append(err1)
        all_debug.extend(dbg1)
        survey_items.extend(area_list)

        matched = [a for a in area_list if _matches(a)]
        if matched:
            val = _safe_area(matched[0])
            if val is not None:
                return val, "조회 성공 (전체조회)", all_errors, all_debug

        # 이 조합에서 데이터가 나왔으면 다른 조합은 시도 안 함
        if area_list:
            break

    # ---------- 3단계: 실패 리포트 ----------
    target_dong_hos = sorted(
        {i.get("hoNm", "") for i in survey_items
         if target_dong and is_match(target_dong, i.get("dongNm", "")) and i.get("hoNm")},
        key=lambda x: (len(x), x)
    ) if target_dong else []

    available_dongs = sorted({(i.get("dongNm") or "").strip() for i in survey_items
                              if (i.get("dongNm") or "").strip()})

    hint = ""
    if target_dong and target_dong_hos:
        preview = ", ".join(target_dong_hos[:20])
        hint = f" | '{target_dong}동'에 존재하는 호수: [{preview}{'...' if len(target_dong_hos)>20 else ''}]"
    elif is_single_building:
        all_hos = sorted({i.get("hoNm","") for i in survey_items if i.get("hoNm")},
                         key=lambda x: (len(x), x))
        if all_hos:
            preview = ", ".join(all_hos[:30])
            hint = f" | 단일동 건물, 존재하는 호수: [{preview}{'...' if len(all_hos)>30 else ''}]"
    elif target_dong and not target_dong_hos and available_dongs:
        hint = f" | '{target_dong}동'이 없습니다. 존재하는 동: {', '.join(available_dongs[:15])}"

    status = f"동/호 매칭 실패 (입력: {target_dong or '?'}동 {target_ho}호){hint}"
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

    is_single = juso.get("is_single_building", False)

    if progress:
        progress.write(
            f"① 매칭({juso.get('_matched_by','-')}): {juso['jibunAddr']} "
            f"| 법정동: {juso['sigunguCd']}{juso['bjdongCd']}, 지번: {juso['bun']}-{juso['ji']} "
            f"| 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}' "
            f"| {'단일동 건물' if is_single else '다동 건물'}"
        )
        if is_single and target_dong:
            progress.info(f"ℹ️ 단일동 아파트로 판단되어 '동' 필터 없이 호수로만 조회합니다.")

    area, status, errors, api_debug = find_dedicated_area(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], target_dong, target_ho,
        is_single_building=is_single,
    )

    debug_bundle = {"juso_debug": juso_debug, "api_debug": api_debug, "errors": errors}

    if area:
        confidence = "확정" if (target_dong and target_ho and not is_single) else "추정"
        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": f"{status} [{confidence}]"}, debug_bundle
    return {"주소": address, "전용면적": "", "상태": status}, debug_bundle

# ============================================================
# 7. UI
# ============================================================

st.title("🏠 건축물 전용면적 조회")
st.caption("💡 **'OO동 OOOO호'** 형식 권장. 단일동 아파트는 동 없이도 조회됩니다.")

addresses = []
for i in range(10):
    address = st.text_input(f"주소 {i + 1}", key=f"address_{i}",
                            placeholder="예: 서울시 서초구 나루터로4길 70-5 101동 1502호")
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
        progress_box = st.empty()
        result, debug_bundle = process_address(address, progress_box)
        results.append(result)
        if result["전용면적"]:
            if "[확정]" in result["상태"]:
                st.success(f"전용면적: {result['전용면적']} ({result['상태']})")
            else:
                st.warning(f"전용면적(추정): {result['전용면적']} ({result['상태']})")
        else:
            st.error(f"조회 실패: {result['상태']}")

        if DEBUG and debug_bundle:
            with st.expander("🔧 디버그 정보"):
                st.json(debug_bundle)
        time.sleep(0.5)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    st.dataframe(results, use_container_width=True, hide_index=True)
    csv_data = pd.DataFrame(results).to_csv(index=False, encoding="utf-8-sig")
    st.download_button("📥 CSV 다운로드", data=csv_data,
                       file_name="전용면적_조회결과.csv", mime="text/csv",
                       use_container_width=True)
