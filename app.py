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
    st.error("⚠️ Streamlit Secrets 설정이 필요합니다. Settings -> Secrets에서 BUILDING_API_KEY와 JUSO_API_KEY를 설정해주세요.")
    st.stop()

BUILDING_API_KEY = st.secrets["BUILDING_API_KEY"]
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]
BUILDING_API_KEY_UNQUOTED = urllib.parse.unquote(BUILDING_API_KEY)

# ============================================================
# 2. 동 / 호 추출 및 숫자가공
# ============================================================

def extract_numbers(value):
    if not value:
        return ""
    nums = re.findall(r"\d+", str(value))
    if nums:
        return str(int(nums[0]))
    return str(value).strip().upper()

def extract_dong_ho(address):
    text = str(address).strip()
    dong = ""
    ho = ""

    # 동, 호 패턴 (예: 103동 701호, 103동701호, 129-2103)
    m = re.search(r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)\s*호?\s*$", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), m.group(1), m.group(2)

    m = re.search(r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)호\s*$", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), m.group(1), m.group(2)

    m = re.search(r"(\d+[A-Za-z가-힣]*)\s*호\s*$", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), "", m.group(1)

    # 띄어쓰기 후 숫자 형태 (예: 일성트루엘 715)
    m = re.search(r"\s(\d{2,5})\s*$", text)
    if m:
        return text[:m.start()].strip(), "", m.group(1)

    return text, dong, ho

# ============================================================
# 3. Juso API (도로명 주소 검색)
# ============================================================

def search_juso(address):
    params = {
        "confmKey": JUSO_API_KEY,
        "currentPage": 1,
        "countPerPage": 10,
        "keyword": address,
        "hstryYn": "N",
        "firstSort": "none",
        "addInfoYn": "Y",
        "resultType": "json"
    }

    try:
        response = requests.get(JUSO_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        common = data.get("results", {}).get("common", {})
        if common.get("errorCode") != "0":
            return None, f"주소 API 오류: {common.get('errorMessage', '')}"

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
            bun = ""
            ji = "0"

        return {
            "sigunguCd": sigunguCd,
            "bjdongCd": bjdongCd,
            "bun": bun.zfill(4),
            "ji": ji.zfill(4),
            "roadAddr": j.get("roadAddr", ""),
            "jibunAddr": jibunAddr
        }, "정상"

    except Exception as e:
        return None, f"주소 API 오류: {e}"

# ============================================================
# 4. 건축물대장 API (다중 페이지 데이터 수집)
# ============================================================

def call_building_api_all_pages(endpoint, params):
    all_items = []
    page = 1
    max_pages = 5

    while page <= max_pages:
        params["pageNo"] = page
        params["numOfRows"] = 1000
        params["_type"] = "json"
        
        # Decoding Key 시도 후 Encoding Key 시도
        params["serviceKey"] = BUILDING_API_KEY_UNQUOTED
        try:
            res = requests.get(f"{BUILDING_API_BASE}/{endpoint}", params=params, timeout=10)
            data = res.json()
        except Exception:
            params["serviceKey"] = BUILDING_API_KEY
            res = requests.get(f"{BUILDING_API_BASE}/{endpoint}", params=params, timeout=10)
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

    return all_items

# ============================================================
# 5. 전용면적 계산 (유연한 매칭 및 Fallback 로직)
# ============================================================

def find_exclusive_area(expos_items, area_items, target_dong, target_ho):
    target_dong_num = extract_numbers(target_dong)
    target_ho_num = extract_numbers(target_ho)

    matched_pks = set()
    fallback_area = None

    # ----------------------------------------------------
    # 1. 전유부(expos_items)에서 매칭 세대 탐색
    # ----------------------------------------------------
    for item in expos_items:
        item_dong_num = extract_numbers(item.get("dongNm", ""))
        item_ho_num = extract_numbers(item.get("hoNm", ""))
        
        # 1-1. 동과 호가 모두 일치하는 경우
        if target_dong_num and target_ho_num:
            if item_dong_num == target_dong_num and item_ho_num == target_ho_num:
                matched_pks.add(str(item.get("mgmBldrgstPk", "")))
                if "area" in item and item["area"]:
                    fallback_area = item["area"]

        # 1-2. 호수만 일치하는 경우 (동 정보 미입력 또는 단일동 건물)
        elif target_ho_num:
            if item_ho_num == target_ho_num:
                matched_pks.add(str(item.get("mgmBldrgstPk", "")))
                if "area" in item and item["area"]:
                    fallback_area = item["area"]

    # 1-3. 동/호수가 미입력된 경우 첫 번째 전유 세대 매칭
    if not target_dong_num and not target_ho_num and expos_items:
        matched_pks.add(str(expos_items[0].get("mgmBldrgstPk", "")))
        if "area" in expos_items[0] and expos_items[0]["area"]:
            fallback_area = expos_items[0]["area"]

    # 동/호 매칭 실패 시 첫 세대 강제 매칭 방지 대신 호수 유연 검색
    if not matched_pks and target_ho_num:
        for item in expos_items:
            item_ho_num = extract_numbers(item.get("hoNm", ""))
            if item_ho_num == target_ho_num:
                matched_pks.add(str(item.get("mgmBldrgstPk", "")))
                if "area" in item and item["area"]:
                    fallback_area = item["area"]

    if not matched_pks:
        return None, "동/호 매칭 실패 (건축물대장 전유부)"

    # ----------------------------------------------------
    # 2. 전유공용면적(area_items)에서 전용면적 합산
    # ----------------------------------------------------
    total_area = 0.0
    found = False

    if area_items:
        for item in area_items:
            item_pk = str(item.get("mgmBldrgstPk", ""))
            gb_cd = str(item.get("exposPubuseGbCd", "")).strip()
            gb_nm = str(item.get("exposPubuseGbCdNm", "")).strip()

            if item_pk in matched_pks:
                # 전유 부분만 추출
                if gb_cd == "1" or "전유" in gb_nm:
                    try:
                        area = float(str(item.get("area", "0")).replace(",", ""))
                        total_area += area
                        found = True
                    except ValueError:
                        pass

    # ----------------------------------------------------
    # 3. 전유공용면적 데이터가 없을 경우 전유부 대장 area로 대체(Fallback)
    # ----------------------------------------------------
    if not found or total_area <= 0:
        if fallback_area:
            try:
                total_area = float(str(fallback_area).replace(",", ""))
                if total_area > 0:
                    return round(total_area, 2), "정상 (전유부 면적 적용)"
            except ValueError:
                pass
        return None, "전유면적 자료 없음"

    return round(total_area, 2), "정상"

# ============================================================
# 6. 메인 프로세스
# ============================================================

def process_address(address, progress=None):
    if not address:
        return {"주소": "", "전용면적": "", "상태": "주소 없음"}

    try:
        base_address, target_dong, target_ho = extract_dong_ho(address)

        if progress:
            progress.write(f"① 주소 검색: {base_address} (동: {target_dong or '미입력'}, 호: {target_ho or '미입력'})")

        juso, msg = search_juso(base_address)
        if not juso:
            return {"주소": address, "전용면적": "", "상태": msg}

        if progress:
            progress.write("② 건축물대장 전유부 조회 중...")

        params = {
            "sigunguCd": juso["sigunguCd"],
            "bjdongCd": juso["bjdongCd"],
            "platGbCd": "0",
            "bun": juso["bun"],
            "ji": juso["ji"]
        }

        expos_items = call_building_api_all_pages("getBrExposInfo", params)
        
        # 산번지 재시도
        if not expos_items:
            params["platGbCd"] = "1"
            expos_items = call_building_api_all_pages("getBrExposInfo", params)

        if not expos_items:
            return {"주소": address, "전용면적": "", "상태": "건축물대장 전유부 없음"}

        if progress:
            progress.write("③ 전유공용면적 데이터 조회 중...")

        area_items = call_building_api_all_pages("getBrExposPubuseAreaInfo", params)

        area, status = find_exclusive_area(expos_items, area_items, target_dong, target_ho)

        if area is None:
            return {"주소": address, "전용면적": "", "상태": status}

        return {"주소": address, "전용면적": f"{area:.2f}㎡", "상태": "조회 성공"}

    except Exception as e:
        return {"주소": address, "전용면적": "", "상태": f"오류: {e}"}

# ============================================================
# 7. UI 화면
# ============================================================

st.title("🏠 건축물 전용면적 조회")
st.write("주소를 입력하면 건축물대장 기준 전용면적을 조회합니다. (동/호수가 없어도 조회 가능)")

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
