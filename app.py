import re
import time
import requests
import urllib.parse
import pandas as pd
import streamlit as st

# ============================================================
# 1. 기본 설정 및 API Key (Colab 방식 규격화)
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

# Colab에서 작동하는 핵심: URL 디코딩된 키 생성
BUILDING_API_KEY_RAW = st.secrets["BUILDING_API_KEY"]
BUILDING_API_KEY_DECODED = urllib.parse.unquote(BUILDING_API_KEY_RAW)
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

# ============================================================
# 2. 주소에서 동 / 호수 파싱
# ============================================================

def clean_num(val):
    if not val:
        return ""
    nums = re.findall(r"\d+", str(val))
    return str(int(nums[0])) if nums else ""

def parse_address_dong_ho(address):
    text = str(address).strip()

    # 1. 129동 2103호 / 129동2103호
    m = re.search(r"(\d+)\s*동\s*(\d+)\s*호?", text)
    if m:
        return text[:m.start()].strip(), m.group(1), m.group(2)

    # 2. 129-2103 형태
    m = re.search(r"(\d{2,3})-(\d{3,4})\b", text)
    if m:
        return text[:m.start()].strip(), m.group(1), m.group(2)

    # 3. 715호 (호수만 있는 경우)
    m = re.search(r"(\d+)\s*호\b", text)
    if m:
        return text[:m.start()].strip(), "", m.group(1)

    # 4. 단순 연속 숫자 (예: 왕십리로 410 129 2103)
    m = re.search(r"\s(\d{2,3})\s+(\d{3,4})\b", text)
    if m:
        return text[:m.start()].strip(), m.group(1), m.group(2)

    # 5. 단순 단일 숫자 (예: 신림로23길 16 715)
    m = re.search(r"\s(\d{3,4})\b", text)
    if m:
        return text[:m.start()].strip(), "", m.group(1)

    return text, "", ""

# ============================================================
# 3. Juso API (법정동코드 및 지번 추출)
# ============================================================

def search_juso(address):
    params = {
        "confmKey": JUSO_API_KEY,
        "currentPage": 1,
        "countPerPage": 1,
        "keyword": address,
        "resultType": "json"
    }

    try:
        res = requests.get(JUSO_API_URL, params=params, timeout=10)
        data = res.json()

        juso_list = data.get("results", {}).get("juso", [])
        if not juso_list:
            return None, "도로명주소 검색 결과 없음"

        j = juso_list[0]
        admCd = j.get("admCd", "")
        if len(admCd) < 10:
            return None, "법정동코드 확인 실패"

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
        }, "정상"

    except Exception as e:
        return None, f"주소 API 오류: {e}"

# ============================================================
# 4. 국토부 건축물대장 API 호출 (Colab 호환)
# ============================================================

def call_api(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    
    # 국토부 API 규격: 번/지는 반드시 4자리 문자열이어야 함
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
# 5. 전용면적 매칭 로직
# ============================================================

def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, target_dong, target_ho):
    t_dong_num = clean_num(target_dong)
    t_ho_num = clean_num(target_ho)

    for platGbCd in ["0", "1", "2"]:
        # 1. 전유부 표제부 정보 (getBrExposInfo)
        expos_list = call_api("getBrExposInfo", sigunguCd, bjdongCd, platGbCd, bun, ji)
        if not expos_list:
            continue

        matched_pk = None

        # 동/호수 매칭
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

        if matched_pk:
            # 2. 전유공용면적 정보 (getBrExposPubuseAreaInfo)
            area_list = call_api("getBrExposPubuseAreaInfo", sigunguCd, bjdongCd, platGbCd, bun, ji)
            total_area = 0.0
            found = False

            for a in area_list:
                if str(a.get("mgmBldrgstPk", "")) == str(matched_pk):
                    # exposPubuseGbCd == '1' (전유부분)
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

    return None, "동/호 전유면적 매칭 실패"

# ============================================================
# 6. 메인 프로세스
# ============================================================

def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}

    base_address, target_dong, target_ho = parse_address_dong_ho(address)

    if progress:
        progress.write(f"① 주소 분석: '{base_address}' | 동: '{target_dong or '없음'}', 호: '{target_ho or '없음'}'")

    juso, msg = search_juso(base_address)
    if not juso:
        return {"주소": address, "전용면적": "", "상태": msg}

    if progress:
        progress.write(f"② 건축물대장 조회 중 (시군구: {juso['sigunguCd']}, 지번: {juso['bun']}-{juso['ji']})...")

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
st.write("주소를 입력하면 세대별 전용면적을 정확히 가져옵니다.")

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
