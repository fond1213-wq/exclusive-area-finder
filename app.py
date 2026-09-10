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
    if not val:
        return ""
    nums = re.findall(r"\d+", str(val))
    return str(int(nums[0])) if nums else ""

def extract_dong_ho(address):
    text = str(address).strip()
    dong, ho = "", ""

    # 1. 동/호 명시 (예: 129동 2103호 / 129동 2103)
    m_dong = re.search(r"(\d+)\s*동", text)
    if m_dong:
        dong = m_dong.group(1)

    m_ho = re.search(r"(\d+)\s*호", text)
    if m_ho:
        ho = m_ho.group(1)

    # 2. 동-호 형태 (예: 129-2103)
    if not dong and not ho:
        m_dash = re.search(r"(\d{2,4})-(\d{3,4})\b", text)
        if m_dash:
            dong = m_dash.group(1)
            ho = m_dash.group(2)

    # 3. 공백 구분 뒤쪽 숫자들
    if not dong and not ho:
        nums = re.findall(r"\b\d+\b", text)
        if len(nums) >= 3:
            dong = nums[-2]
            ho = nums[-1]
        elif len(nums) == 2:
            ho = nums[-1]

    return dong, ho

def sanitize_base_address(address):
    """동/호수 및 상세주소를 제거하고 도로명+건물번호만 남김"""
    text = str(address).strip()
    text = re.sub(r"\d+\s*동.*", "", text)
    text = re.sub(r"\d+-\d+.*", "", text)
    text = re.sub(r"\d+\s*호.*", "", text)
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
    # 1차: 원본 주소 전체 검색
    res = search_juso_single(address)
    if res:
        return res, "정상 (1차 원문검색)"

    # 2차: 동/호수 제거 후 기본 도로명주소로 검색
    clean_addr = sanitize_base_address(address)
    if clean_addr and clean_addr != address:
        res = search_juso_single(clean_addr)
        if res:
            return res, "정상 (2차 기본주소 정제검색)"

    return None, "도로명주소 검색 결과 없음"

# ============================================================
# 4. 건축물대장 API 호출
# ============================================================

def call_api(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    
    bun_str = str(bun).zfill(4)
    ji_str = str(ji).zfill(4)

    params = {
        "serviceKey": BUILDING_API_KEY_DECODED,
        "sigunguCd": sigunguCd,
        "bjdongCd": bjdongCd,
        "platGbCd": platGbCd,
        "bun": bun_str,
        "ji": ji_str,
        "numOfRows": "1000",
        "pageNo": "1",
        "_type": "json"
    }

    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        items = data.get("response", {}).get("body", {}).get("items", {})
        if not items:
            return []
        
        item_list = items.get("item", [])
        if isinstance(item_list, dict):
            return [item_list]
        return item_list
    except Exception:
        return []

# ============================================================
# 5. 전용면적 정밀 매칭 엔진
# ============================================================

def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, target_dong, target_ho):
    t_dong_num = clean_num(target_dong)
    t_ho_num = clean_num(target_ho)

    # 지번 시도 조합 (기본 지번 -> 부번 0으로 변경 시도)
    ji_variants = [ji]
    if ji != "0":
        ji_variants.append("0")

    for current_ji in ji_variants:
        for platGbCd in ["0", "1", "2"]:
            # 1. 표제부/전유부 기본목록 (getBrExposInfo)
            expos_list = call_api("getBrExposInfo", sigunguCd, bjdongCd, platGbCd, bun, current_ji)
            if not expos_list:
                continue

            matched_pk = None

            # 동 + 호 정규화 매칭
            for item in expos_list:
                i_dong = clean_num(item.get("dongNm", ""))
                i_ho = clean_num(item.get("hoNm", ""))

                if t_dong_num and t_ho_num:
                    if i_dong == t_dong_num and i_ho == t_ho_num:
                        matched_pk = item.get("mgmBldrgstPk")
                        break
                elif t_ho_num:
                    if i_ho == t_ho_num:
                        matched_pk = item.get("mgmBldrgstPk")
                        break

            # 동 매칭 실패 시 호수만 재시도
            if not matched_pk and t_ho_num:
                for item in expos_list:
                    i_ho = clean_num(item.get("hoNm", ""))
                    if i_ho == t_ho_num:
                        matched_pk = item.get("mgmBldrgstPk")
                        break

            # 동/호 입력이 없었던 경우 첫 항목
            if not matched_pk and not t_dong_num and not t_ho_num:
                matched_pk = expos_list[0].get("mgmBldrgstPk")

            if matched_pk:
                # 2. 전유공용면적 정보 (getBrExposPubuseAreaInfo)
                area_list = call_api("getBrExposPubuseAreaInfo", sigunguCd, bjdongCd, platGbCd, bun, current_ji)
                total_area = 0.0
                found = False

                for a in area_list:
                    if str(a.get("mgmBldrgstPk", "")) == str(matched_pk):
                        gb_cd = str(a.get("exposPubuseGbCd", "")).strip()
                        gb_nm = str(a.get("exposPubuseGbCdNm", "")).strip()

                        if gb_cd == "1" or "전유" in gb_nm:
                            try:
                                total_area += float(str(a.get("area", "0")).replace(",", ""))
                                found = True
                            except ValueError:
                                pass

                if found and total_area > 0:
                    return round(total_area, 2), "조회 성공 (전용면적)"

                # 전유공용대장 조회가 안 될 경우 전유부 area 직접 사용
                for item in expos_list:
                    if str(item.get("mgmBldrgstPk", "")) == str(matched_pk):
                        try:
                            val = float(str(item.get("area", "0")).replace(",", ""))
                            if val > 0:
                                return round(val, 2), "조회 성공 (전유부 기준)"
                        except ValueError:
                            pass

    return None, "동/호 전유면적 매칭 실패"

# ============================================================
# 6. 프로세스 실행
# ============================================================

def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}

    # 1. 2단계 주소 검색
    juso, msg = search_juso(address)
    if not juso:
        return {"주소": address, "전용면적": "", "상태": msg}

    # 2. 입력값에서 동/호수 파싱
    target_dong, target_ho = extract_dong_ho(address)

    if progress:
        progress.write(f"① 지번 확인: {juso['jibunAddr']} (법정동: {juso['sigunguCd']}{juso['bjdongCd']}, 지번: {juso['bun']}-{juso['ji']}) | 인식된 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}'")

    # 3. 건축물대장 API 조회
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
st.write("주소를 입력하시면 동/호수 매칭을 거쳐 세대별 전용면적을 가져옵니다.")

addresses = []
for i in range(10):
    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="예: 서울특별시 성동구 왕십리로 410 129동 2103호"
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
