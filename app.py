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
# 2. 정규화 / 파싱 (is_match 개선: A동 같은 영문 동도 지원)
# ============================================================

def normalize_name(s):
    """'제129동' / '129동' / '0129' / 'A동' → 숫자/문자만 남김"""
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
    # 동: 숫자 or 영문자 + '동'
    m_dong = re.search(r"([0-9A-Za-z]+)\s*동", text)
    if m_dong:
        dong = m_dong.group(1)
    m_ho = re.search(r"(\d+)\s*호", text)
    if m_ho:
        ho = m_ho.group(1)
    if not dong and not ho:
        m_dash = re.search(r"(\d{2,4})-(\d{3,4})\b", text)
        if m_dash:
            dong = m_dash.group(1)
            ho = m_dash.group(2)
    if not ho:
        tokens = text.split()
        if tokens and tokens[-1].isdigit() and len(tokens[-1]) >= 3:
            ho = tokens[-1]
    return dong, ho

def sanitize_road_address(address):
    text = str(address).strip()
    text = re.sub(r"[0-9A-Za-z]+\s*동.*", "", text)
    text = re.sub(r"\d+\s*호.*", "", text)
    text = re.sub(r"\d+-\d+.*", "", text)
    m = re.search(r"([가-힣a-zA-Z0-9\s]+(?:로|길|대로)\s*\d+(?:-\d+)?)", text)
    if m:
        return m.group(1).strip()
    return text.strip()

# ============================================================
# 3. Juso API
# ============================================================

def search_juso_single(keyword):
    params = {
        "confmKey": JUSO_API_KEY, "currentPage": 1, "countPerPage": 5,
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
            return None, "Juso API 응답이 JSON이 아닙니다", dbg
        common = data.get("results", {}).get("common", {})
        err_code = common.get("errorCode", "")
        if err_code and err_code != "0":
            return None, f"Juso API 오류 [{err_code}] {common.get('errorMessage','')}", dbg
        juso_list = data.get("results", {}).get("juso", [])
        if not juso_list:
            return None, "검색 결과 없음", dbg
        j = juso_list[0]
        admCd = j.get("admCd", "")
        if len(admCd) < 10:
            return None, "admCd 오류", dbg
        lnbr_mnnm = j.get("lnbrMnnm", "")
        lnbr_slno = j.get("lnbrSlno", "")
        mt_yn = j.get("mtYn", "0")
        bun = str(int(lnbr_mnnm)) if lnbr_mnnm and lnbr_mnnm.isdigit() else "0"
        ji = str(int(lnbr_slno)) if lnbr_slno and lnbr_slno.isdigit() else "0"
        plat_gb_cd = "1" if str(mt_yn) == "1" else "0"
        dbg["raw_juso_item"] = j
        return {
            "sigunguCd": admCd[:5], "bjdongCd": admCd[5:10],
            "bun": bun, "ji": ji, "platGbCd": plat_gb_cd,
            "jibunAddr": j.get("jibunAddr", ""), "bdNm": j.get("bdNm", ""),
        }, "정상", dbg
    except requests.exceptions.RequestException as e:
        return None, f"Juso API 요청 실패: {e}", dbg

def search_juso(address):
    clean_addr = sanitize_road_address(address)
    res, msg, dbg = search_juso_single(clean_addr)
    if res:
        return res, msg, dbg
    res2, msg2, dbg2 = search_juso_single(address)
    if res2:
        return res2, msg2, dbg2
    return None, f"{msg} / 재시도: {msg2}", {"1차": dbg, "2차": dbg2}

# ============================================================
# 4. 국토부 API 호출
# ============================================================

def call_api_all_pages(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji,
                       match_check=None, extra_params=None,
                       collect_all=False):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    bun_str = str(bun).zfill(4)
    ji_str = str(ji).zfill(4)

    all_items = []
    page = 1
    error_msg = None
    debug_snippets = []
    ROWS_PER_PAGE = 1000
    MAX_PAGES = 40
    total_pages_needed = None

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
            res = requests.get(url, params=params, timeout=20)
            raw_text = res.text or ""
            if not raw_text.strip():
                time.sleep(1.0)
                res = requests.get(url, params=params, timeout=20)
                raw_text = res.text or ""
            try:
                data = res.json()
            except ValueError:
                error_msg = f"[{endpoint}] JSON 파싱 실패 (HTTP {res.status_code})"
                debug_snippets.append({
                    "endpoint": endpoint, "status_code": res.status_code,
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
            time.sleep(0.15)

        except requests.exceptions.RequestException as e:
            error_msg = f"[{endpoint}] 요청 실패: {e}"
            break

    return all_items, error_msg, debug_snippets

# ============================================================
# 5. 매칭 엔진 (필터 조합 전면 재정비)
# ============================================================

def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, plat_gb_cd_hint, target_dong, target_ho):
    if not target_ho:
        return None, "호수를 인식하지 못했습니다. '101동 2103호' 형식으로 입력해주세요.", [], []

    ji_variants = [ji]
    if ji != "0":
        ji_variants.append("0")

    plat_gb_candidates = [plat_gb_cd_hint] + [c for c in ["0", "1", "2"] if c != plat_gb_cd_hint]

    all_errors = []
    all_debug = []

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
        d = item.get("dongNm", "")
        h = item.get("hoNm", "")
        if target_dong and target_ho:
            return is_match(target_dong, d) and is_match(target_ho, h)
        return is_match(target_ho, h)

    # ★★★ 필터 조합 대폭 확장 (실제 데이터 형식 "129동" + "2103" 포함) ★★★
    filter_sets = []
    if target_dong and target_ho:
        filter_sets = [
            {"dongNm": f"{target_dong}동", "hoNm": str(target_ho)},   # ← 실제 데이터 형식
            {"dongNm": str(target_dong), "hoNm": str(target_ho)},
            {"dongNm": f"{target_dong}동", "hoNm": f"{target_ho}호"},
            {"dongNm": str(target_dong), "hoNm": f"{target_ho}호"},
        ]
    else:
        filter_sets = [{"hoNm": str(target_ho)}]

    # ---------- 1단계: 필터 시도 ----------
    for extra in filter_sets:
        for platGbCd in plat_gb_candidates:
            for current_ji in ji_variants:
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

    # ---------- 2단계: 전체조회 후 클라이언트 매칭 ----------
    survey_items = []   # 매칭 실패 시 사용자에게 보여줄 정보
    for platGbCd in plat_gb_candidates:
        for current_ji in ji_variants:
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

            # platGbCd=0에서 이미 다 가져왔으면 다른 platGbCd는 스킵
            if area_list:
                break

    # ---------- 3단계: 매칭 실패 → 어떤 동/호가 있는지 리포트 ----------
    available_dongs = sorted({i.get("dongNm", "") for i in survey_items if i.get("dongNm")})
    # 타겟 동의 호수 목록
    target_dong_hos = sorted(
        {i.get("hoNm", "") for i in survey_items
         if is_match(target_dong, i.get("dongNm", "")) and i.get("hoNm")},
        key=lambda x: (len(x), x)
    ) if target_dong else []

    hint = ""
    if target_dong and target_dong_hos:
        preview = ", ".join(target_dong_hos[:20])
        hint = f" | '{target_dong}동'에 존재하는 호수: [{preview}{'...' if len(target_dong_hos)>20 else ''}]"
    elif target_dong and not target_dong_hos:
        hint = f" | '{target_dong}동'이 이 건물에 없습니다. 존재하는 동: {', '.join(available_dongs[:15])}"

    status = f"동/호 매칭 실패 (입력: {target_dong}동 {target_ho}호){hint}"
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

    if progress:
        progress.write(
            f"① 지번: {juso['jibunAddr']} | 법정동: {juso['sigunguCd']}{juso['bjdongCd']} "
            f"| 지번: {juso['bun']}-{juso['ji']} (platGbCd={juso['platGbCd']}) "
            f"| 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}'"
        )
        if not target_dong:
            progress.warning("⚠️ 주소에 '동'이 없어 호수만으로 매칭합니다. '101동 2103호' 형식 권장.")
        if not target_ho:
            progress.warning("⚠️ 주소에 '호'가 없어 조회할 수 없습니다.")

    area, status, errors, api_debug = find_dedicated_area(
        juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"],
        juso["platGbCd"], target_dong, target_ho,
    )

    debug_bundle = {"juso_debug": juso_debug, "api_debug": api_debug, "errors": errors}

    if area:
        confidence = "확정" if (target_dong and target_ho) else "추정"
        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": f"{status} [{confidence}]"}, debug_bundle
    return {"주소": address, "전용면적": "", "상태": status}, debug_bundle

# ============================================================
# 7. UI
# ============================================================

st.title("🏠 건축물 전용면적 조회")
st.caption("💡 **'OO동 OOOO호'** 형식으로 입력하세요. (예: 서울특별시 성동구 왕십리로410 129동 1605호)")

addresses = []
for i in range(10):
    address = st.text_input(f"주소 {i + 1}", key=f"address_{i}",
                            placeholder="예: 서울특별시 성동구 왕십리로410 129동 1605호")
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
        time.sleep(0.2)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    st.dataframe(results, use_container_width=True, hide_index=True)
    csv_data = pd.DataFrame(results).to_csv(index=False, encoding="utf-8-sig")
    st.download_button("📥 CSV 다운로드", data=csv_data,
                       file_name="전용면적_조회결과.csv", mime="text/csv",
                       use_container_width=True)
