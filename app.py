import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st

# ============================================================
# 1. 기본 설정 및 API Key
# ============================================================

st.set_page_config(
    page_title="전용면적 조회",
    page_icon="🏠",
    layout="wide"
)

BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

if "BUILDING_API_KEY" not in st.secrets or "JUSO_API_KEY" not in st.secrets:
    st.error("⚠️ Streamlit Secrets 설정이 필요합니다.")
    st.stop()

BUILDING_API_KEY_RAW = st.secrets["BUILDING_API_KEY"]
BUILDING_API_KEY_DECODED = urllib.parse.unquote(BUILDING_API_KEY_RAW)
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

# ============================================================
# 2. 파싱 및 정규화 함수
# ============================================================

def clean_num(val):
    """문자열에서 숫자만 추출하여 정수형 문자열로 반환 (예: '제129동' -> '129')"""
    if not val:
        return ""
    nums = re.findall(r"\d+", str(val))
    return str(int(nums[0])) if nums else ""

def extract_dong_ho(address):
    """주소 원문에서 동, 호수 완벽 추출"""
    text = str(address).strip()
    dong, ho = "", ""

    # 1. '숫자+동' 형태 (예: 129동, 103동)
    m_dong = re.search(r"(\d+)\s*동", text)
    if m_dong:
        dong = m_dong.group(1)

    # 2. '숫자+호' 형태 (예: 2103호, 701호)
    m_ho = re.search(r"(\d+)\s*호", text)
    if m_ho:
        ho = m_ho.group(1)

    # 3. '동/호' 문구가 없는 '동-호' 형태 (예: 129-2103)
    if not dong and not ho:
        m_dash = re.search(r"(\d{2,4})-(\d{3,4})\b", text)
        if m_dash:
            dong = m_dash.group(1)
            ho = m_dash.group(2)

    # 4. 동은 없고 호수 숫자만 붙어 있는 경우 (예: 일성트루엘 715)
    if not ho:
        tokens = text.split()
        if tokens and tokens[-1].isdigit() and len(tokens[-1]) >= 3:
            ho = tokens[-1]

    return dong, ho

def sanitize_road_address(address):
    """
    '신반포로33길 15' 처럼 도로명+건물번호만 잘라냄 (아파트명, 동/호수 완벽 제거)
    """
    text = str(address).strip()
    
    # 동/호수 및 아파트 단지명 이후 문구 제거
    text = re.sub(r"\d+\s*동.*", "", text)
    text = re.sub(r"\d+\s*호.*", "", text)
    text = re.sub(r"\d+-\d+.*", "", text)
    
    # '도로명 + 건물번호' 정밀 패턴 추출 (예: 신반포로33길 15, 왕십리로 410)
    m = re.search(r"([가-힣A-Za-z0-9\s]+(?:로|길|대로)\s*\d+(?:-\d+)?)", text)
    if m:
        return m.group(1).strip()
    
    return text.strip()

# ============================================================
# 3. Juso API (2단계 폴백 검색)
# ============================================================

def search_juso_single(keyword):
    params = {
        "confmKey": JUSO_API_KEY,
        "currentPage": 1,
        "countPerPage": 1,
        "keyword": keyword,
        "resultType": "json"
    }
    try:
        res = requests.get(JUSO_API_URL, params=params, timeout=10)
        data = res.json()
        juso_list = data.get("results", {}).get("juso", [])
        if juso_list:
            j = juso_list[0]
            admCd = j.get("admCd", "")
            if len(admCd) >= 10:
                sigunguCd = admCd[:5]
                bjdongCd = admCd[5:10]
                jibunAddr = j.get("jibunAddr", "")

                jibun_match = re.search(r"\s(\d+)(?:-(\d+))?(?:\s|$)", jibunAddr)
                if jibun_match:
                    bun = jibun_match.group(1)
                    ji = jibun_match.group(2) or "0"
                else:
                    bun = "0"
                    ji = "0"

                return {
                    "sigunguCd": sigunguCd,
                    "bjdongCd": bjdongCd,
                    "bun": bun,
                    "ji": ji,
                    "roadAddr": j.get("roadAddr", ""),
                    "jibunAddr": jibunAddr
                }
    except Exception:
        pass
    return None

def search_juso(address):
    # 1차: 정제된 도로명 + 건물번호로 검색
    clean_addr = sanitize_road_address(address)
    res = search_juso_single(clean_addr)
    if res:
        return res, "정상"

    # 2차: 실패 시 입력 원문 전체로 재시도
    res = search_juso_single(address)
    if res:
        return res, "정상"

    return None, "도로명주소 검색 결과 없음"

# ============================================================
# 4. 국토부 건축물대장 API 호출 (페이지네이션 전수조회)
# ============================================================

def call_api_all_pages(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    bun_str = str(bun).zfill(4)
    ji_str = str(ji).zfill(4)

    all_items = []
    page = 1

    while page <= 10:  # 최대 10,000건 전수 탐색
        params = {
            "serviceKey": BUILDING_API_KEY_DECODED,
            "sigunguCd": sigunguCd,
            "bjdongCd": bjdongCd,
            "platGbCd": platGbCd,
            "bun": bun_str,
            "ji": ji_str,
            "numOfRows": "1000",
            "pageNo": str(page),
            "_type": "json"
        }

        try:
            res = requests.get(url, params=params, timeout=10)
            data = res.json()
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

            total_count = int(body.get("totalCount", 0))
            if len(all_items) >= total_count:
                break

            page += 1
        except Exception:
            break

    return all_items

# ============================================================
# 5. 전용면적 정밀 매칭 엔진 (동/호 유연한 비교)
# ============================================================

def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, target_dong, target_ho):
    t_dong_num = clean_num(target_dong)
    t_ho_num = clean_num(target_ho)

    ji_variants = [ji]
    if ji != "0":
        ji_variants.append("0")

    for current_ji in ji_variants:
        for platGbCd in ["0", "1", "2"]:
            # 1. 전유공용면적 API 조회 (getBrExposPubuseAreaInfo)
            area_list = call_api_all_pages("getBrExposPubuseAreaInfo", sigunguCd, bjdongCd, platGbCd, bun, current_ji)
            
            if area_list:
                # 1단계: 동 + 호 일치
                if t_dong_num and t_ho_num:
                    for a in area_list:
                        i_dong = clean_num(a.get("dongNm", ""))
                        i_ho = clean_num(a.get("hoNm", ""))
                        gb_cd = str(a.get("exposPubuseGbCd", "")).strip()
                        gb_nm = str(a.get("exposPubuseGbCdNm", "")).strip()

                        if (gb_cd == "1" or "전유" in gb_nm) and i_dong == t_dong_num and i_ho == t_ho_num:
                            try:
                                val = float(str(a.get("area", "0")).replace(",", ""))
                                if val > 0:
                                    return round(val, 2), "조회 성공"
                            except ValueError:
                                pass

                # 2단계: 호수 기준 유연 매칭 (동 명칭 불일치 대응)
                if t_ho_num:
                    for a in area_list:
                        i_ho = clean_num(a.get("hoNm", ""))
                        gb_cd = str(a.get("exposPubuseGbCd", "")).strip()
                        gb_nm = str(a.get("exposPubuseGbCdNm", "")).strip()

                        if (gb_cd == "1" or "전유" in gb_nm) and i_ho == t_ho_num:
                            try:
                                val = float(str(a.get("area", "0")).replace(",", ""))
                                if val > 0:
                                    return round(val, 2), "조회 성공 (호수 기준)"
                            except ValueError:
                                pass

            # 2. 전유부 기본 목록 API 조회 (getBrExposInfo)
            expos_list = call_api_all_pages("getBrExposInfo", sigunguCd, bjdongCd, platGbCd, bun, current_ji)
            if expos_list:
                if t_dong_num and t_ho_num:
                    for item in expos_list:
                        i_dong = clean_num(item.get("dongNm", ""))
                        i_ho = clean_num(item.get("hoNm", ""))
                        if i_dong == t_dong_num and i_ho == t_ho_num:
                            try:
                                val = float(str(item.get("area", "0")).replace(",", ""))
                                if val > 0:
                                    return round(val, 2), "조회 성공 (전유부 기준)"
                            except ValueError:
                                pass

                if t_ho_num:
                    for item in expos_list:
                        i_ho = clean_num(item.get("hoNm", ""))
                        if i_ho == t_ho_num:
                            try:
                                val = float(str(item.get("area", "0")).replace(",", ""))
                                if val > 0:
                                    return round(val, 2), "조회 성공 (전유부 호수 기준)"
                            except ValueError:
                                pass

    return None, "동/호 전유면적 매칭 실패"

# ============================================================
# 6. 프로세스 실행
# ============================================================

def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}

    # 1. 입력 원문에서 동/호수 파싱
    target_dong, target_ho = extract_dong_ho(address)

    # 2. 정제된 지번으로 도로명주소 API 검색
    juso, msg = search_juso(address)
    if not juso:
        return {"주소": address, "전용면적": "", "상태": msg}

    if progress:
        progress.write(f"① 지번 확인: {juso['jibunAddr']} (법정동: {juso['sigunguCd']}{juso['bjdongCd']}, 지번: {juso['bun']}-{juso['ji']}) | 인식된 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}'")

    # 3. 국토부 건축물대장 API 매칭
    area, status = find_dedicated_area(
        juso["sigunguCd"],
        juso["bjdongCd"],
        juso["bun"],
        juso["ji"],
        target_dong,
        target_ho
    )

    if area:
        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": status}
    else:
        return {"주소": address, "전용면적": "", "상태": status}

# ============================================================
# 7. UI 화면
# ============================================================

st.title("🏠 건축물 전용면적 조회")
st.write("주소를 입력하시면 세대별 전용면적을 정확하게 가져옵니다.")

addresses = []
for i in range(10):
    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="예: 서울특별시 성동구 왕십리로410 129동 2103호"
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
        progress_box = st.empty()

        result = process_address(address, progress_box)
        results.append(result)

        if result["전용면적"]:
            st.success(f"전용면적: {result['전용면적']} ({result['상태']})")
        else:
            st.error(f"조회 실패: {result['상태']}")

        time.sleep(0.2)

    st.markdown("---")
    st.subheader("📋 조회 결과")
    st.dataframe(results, use_container_width=True, hide_index=True)

    result_df = pd.DataFrame(results)
    csv_data = result_df.to_csv(index=False, encoding="utf-8-sig")

    st.download_button(
        "📥 결과 CSV 다운로드",
        data=csv_data,
        file_name="전용면적_조회결과.csv",
        mime="text/csv",
        use_container_width=True
    )
