import re
import time
import requests
import streamlit as st
from urllib.parse import unquote


# ============================================================
# 1. 기본 설정
# ============================================================

st.set_page_config(
    page_title="전용면적 조회",
    page_icon="🏠",
    layout="wide"
)


# ============================================================
# 2. API KEY
# ============================================================

BUILDING_API_KEY = st.secrets["BUILDING_API_KEY"]
JUSO_API_KEY = st.secrets["JUSO_API_KEY"]

BUILDING_API_KEY = unquote(BUILDING_API_KEY)
JUSO_API_KEY = unquote(JUSO_API_KEY)


BUILDING_API_BASE = (
    "https://apis.data.go.kr/1613000/BldRgstHubService"
)

JUSO_API_URL = (
    "https://business.juso.go.kr/addrlink/addrLinkApi.do"
)


# ============================================================
# 3. 문자열 정리
# ============================================================

def normalize_text(value):

    if value is None:
        return ""

    value = str(value).strip()
    value = value.replace(" ", "")
    value = value.replace("　", "")

    return value.upper()


def normalize_dong(value):

    value = normalize_text(value)

    if not value:
        return ""

    value = re.sub(r"^제", "", value)
    value = value.replace("동", "")

    return value


def normalize_ho(value):

    value = normalize_text(value)

    if not value:
        return ""

    value = value.replace("호", "")

    return value


# ============================================================
# 4. 주소에서 동 / 호 추출
# ============================================================

def extract_dong_ho(address):

    text = str(address).strip()

    dong = ""
    ho = ""

    # 101동 1203호
    m = re.search(
        r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)\s*호?\s*$",
        text,
        re.IGNORECASE
    )

    if m:

        dong = m.group(1)
        ho = m.group(2)

        text = text[:m.start()].strip()

        return text, dong, ho


    # 101동1203호
    m = re.search(
        r"(\d+)\s*동\s*(\d+[A-Za-z가-힣]*)호\s*$",
        text,
        re.IGNORECASE
    )

    if m:

        dong = m.group(1)
        ho = m.group(2)

        text = text[:m.start()].strip()

        return text, dong, ho


    # 1203호
    m = re.search(
        r"(\d+[A-Za-z가-힣]*)\s*호\s*$",
        text,
        re.IGNORECASE
    )

    if m:

        ho = m.group(1)

        text = text[:m.start()].strip()

        return text, "", ho


    # 마지막 숫자를 호수로 간주
    # 예: 일성트루엘 715
    m = re.search(
        r"\s(\d{2,5})\s*$",
        text
    )

    if m:

        ho = m.group(1)

        text = text[:m.start()].strip()


    return text, dong, ho


# ============================================================
# 5. Juso 주소 검색
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

        response = requests.get(
            JUSO_API_URL,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        common = (
            data
            .get("results", {})
            .get("common", {})
        )


        if common.get("errorCode") != "0":

            return None, (
                "주소 API 오류: "
                + str(common.get("errorMessage", ""))
            )


        juso_list = (
            data
            .get("results", {})
            .get("juso", [])
        )


        if not juso_list:

            return None, "도로명주소 검색 결과 없음"


        j = juso_list[0]


        # 법정동코드

        admCd = j.get("admCd", "")

        if len(admCd) < 10:

            return None, "법정동코드 확인 실패"


        sigunguCd = admCd[:5]

        bjdongCd = admCd[5:10]


        # 지번

        jibunAddr = j.get("jibunAddr", "")


        jibun_match = re.search(
            r"\s(\d+)(?:-(\d+))?(?:\s|$)",
            jibunAddr
        )


        if jibun_match:

            bun = jibun_match.group(1)

            ji = (
                jibun_match.group(2)
                or "0"
            )

        else:

            bun = ""

            ji = "0"


        return {

            "sigunguCd": sigunguCd,

            "bjdongCd": bjdongCd,

            "bun": bun.zfill(4),

            "ji": ji.zfill(4),

            "roadAddr": j.get("roadAddr", ""),

            "jibunAddr": jibunAddr,

            "bdNm": j.get("bdNm", ""),

            "admCd": admCd

        }, "정상"


    except requests.exceptions.Timeout:

        return None, "주소 API 시간 초과"


    except Exception as e:

        return None, f"주소 API 오류: {e}"


# ============================================================
# 6. 건축물대장 전유부
# ============================================================

def get_expos_info(
    sigunguCd,
    bjdongCd,
    platGbCd,
    bun,
    ji
):

    url = (
        BUILDING_API_BASE
        + "/getBrExposInfo"
    )


    params = {

        "serviceKey": BUILDING_API_KEY,

        "sigunguCd": sigunguCd,

        "bjdongCd": bjdongCd,

        "platGbCd": platGbCd,

        "bun": bun,

        "ji": ji,

        "pageNo": 1,

        "numOfRows": 100,

        "_type": "json"
    }


    try:

        response = requests.get(
            url,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()


        body = (
            data
            .get("response", {})
            .get("body", {})
        )


        items = (
            body
            .get("items", {})
            .get("item", [])
        )


        if isinstance(items, dict):

            items = [items]


        return items


    except requests.exceptions.Timeout:

        raise Exception(
            "건축물대장 전유부 API 시간 초과"
        )


    except Exception as e:

        raise Exception(
            f"건축물대장 전유부 API 오류: {e}"
        )


# ============================================================
# 7. 전유공용면적
# ============================================================

def get_area_info(sigunguCd, bjdongCd, platGbCd, bun, ji):

    url = BUILDING_API_BASE + "/getBrExposPubuseAreaInfo"

    params = {
        "serviceKey": BUILDING_API_KEY,
        "sigunguCd": str(sigunguCd),
        "bjdongCd": str(bjdongCd),
        "platGbCd": str(platGbCd),
        "bun": str(bun).zfill(4),
        "ji": str(ji).zfill(4),
        "pageNo": 1,
        "numOfRows": 100,
        "_type": "json"
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        header = data.get("response", {}).get("header", {})
        body = data.get("response", {}).get("body", {})

        result_code = str(header.get("resultCode", ""))
        result_msg = str(header.get("resultMsg", ""))

        if result_code not in ("00", "0", ""):
            raise Exception(
                f"전유공용면적 API 오류: {result_code} / {result_msg}"
            )

        items = body.get("items", {}).get("item", [])

        if isinstance(items, dict):
            items = [items]

        return items

    except requests.exceptions.Timeout:
        raise Exception("전유공용면적 API 시간 초과")

    except requests.exceptions.RequestException as e:
        raise Exception(f"전유공용면적 HTTP 오류: {e}")

    except Exception as e:
        raise Exception(str(e))


# ============================================================
# 8. 전용면적 매칭
# ============================================================

def find_exclusive_area(
    expos_items,
    area_items,
    target_dong,
    target_ho
):

    target_dong = normalize_dong(target_dong)
    target_ho = normalize_ho(target_ho)

    # --------------------------------------------------------
    # 1. 전유부에서 해당 호 찾기
    # --------------------------------------------------------

    candidates = []

    for item in expos_items:

        item_dong = normalize_dong(
            item.get("dongNm", "")
        )

        item_ho = normalize_ho(
            item.get("hoNm", "")
        )

        # 호수가 있으면 반드시 일치
        if target_ho and item_ho != target_ho:
            continue

        # 동이 있으면 동도 일치
        if target_dong and item_dong != target_dong:
            continue

        candidates.append(item)


    # 동/호를 못 찾았으면 호만 다시 검색
    if not candidates and target_ho:

        for item in expos_items:

            item_ho = normalize_ho(
                item.get("hoNm", "")
            )

            if item_ho == target_ho:
                candidates.append(item)


    if not candidates:

        return None, "동/호 매칭 실패", ""


    candidate = candidates[0]

    pk = str(
        candidate.get(
            "mgmBldrgstPk",
            ""
        )
    )


    candidate_dong = normalize_dong(
        candidate.get("dongNm", "")
    )

    candidate_ho = normalize_ho(
        candidate.get("hoNm", "")
    )


    # --------------------------------------------------------
    # 2. 전유공용면적 자료에서 대상 호 찾기
    # --------------------------------------------------------

    matched_items = []

    for item in area_items:

        item_dong = normalize_dong(
            item.get("dongNm", "")
        )

        item_ho = normalize_ho(
            item.get("hoNm", "")
        )

        item_pk = str(
            item.get(
                "mgmBldrgstPk",
                ""
            )
        )


        # ------------------------------------------
        # 동 + 호가 일치하면 우선 채택
        # ------------------------------------------

        if candidate_dong and candidate_ho:

            if (
                item_dong == candidate_dong
                and item_ho == candidate_ho
            ):

                matched_items.append(item)

                continue


        # ------------------------------------------
        # 동이 없는 경우 호만 비교
        # ------------------------------------------

        if candidate_ho:

            if item_ho == candidate_ho:

                matched_items.append(item)

                continue


        # ------------------------------------------
        # PK가 같으면 채택
        # ------------------------------------------

        if pk and item_pk:

            if pk == item_pk:

                matched_items.append(item)


    # --------------------------------------------------------
    # 3. 동/호/PK가 모두 안 맞는 경우
    #    후보를 직접 찾기 위한 추가 검색
    # --------------------------------------------------------

    if not matched_items:

        for item in area_items:

            item_ho = normalize_ho(
                item.get("hoNm", "")
            )

            if target_ho and item_ho == target_ho:

                matched_items.append(item)


    if not matched_items:

        return None, "전유공용면적 동/호 매칭 실패", pk


    # --------------------------------------------------------
    # 4. 여기서 '전유' 자료만 골라냄
    # --------------------------------------------------------

    exclusive_items = []

    for item in matched_items:

        gb_cd = normalize_text(
            item.get(
                "exposPubuseGbCd",
                ""
            )
        )

        gb_nm = normalize_text(
            item.get(
                "exposPubuseGbCdNm",
                ""
            )
        )


        # 여러 형태를 허용
        is_exclusive = (

            gb_cd in (
                "1",
                "01"
            )

            or

            "전유" in gb_nm
        )


        if is_exclusive:

            exclusive_items.append(item)


    # --------------------------------------------------------
    # 5. 전유 구분값이 예상과 다른 경우
    #    면적이 있는 첫 번째 후보를 사용
    # --------------------------------------------------------

    if not exclusive_items:

        for item in matched_items:

            area_value = item.get(
                "area",
                ""
            )

            if area_value not in (
                None,
                "",
                "0",
                0
            ):

                exclusive_items.append(item)


    if not exclusive_items:

        return None, "전용면적 값 없음", pk


    # --------------------------------------------------------
    # 6. 면적 계산
    # --------------------------------------------------------

    total_area = 0

    for item in exclusive_items:

        try:

            area = float(
                str(
                    item.get(
                        "area",
                        "0"
                    )
                )
                .replace(",", "")
                .strip()
            )

            total_area += area

        except:

            continue


    if total_area <= 0:

        return None, "전용면적 값 없음", pk


    return (
        round(total_area, 2),
        "정상",
        pk
    )
            
                 
      

    


# ============================================================
# 9. 한 주소 조회
# ============================================================

def process_address(address, progress=None):

    address = str(address).strip()


    if not address:

        return {
            "주소": "",
            "전용면적": "",
            "상태": "주소 없음"
        }


    try:

        # -------------------------
        # 동 / 호 분리
        # -------------------------

        base_address, target_dong, target_ho = (
            extract_dong_ho(address)
        )


        if progress:

            progress.write(
                f"① 주소정보 검색 중: {base_address}"
            )


        # 도로명주소만 추출

        m = re.search(
            r"^(.+\s+\d+(?:-\d+)?)\s+",
            base_address
        )


        if m:

            juso_search_address = m.group(1)

        else:

            juso_search_address = base_address


        # -------------------------
        # Juso
        # -------------------------

        juso, msg = search_juso(
            juso_search_address
        )


        if juso is None:

            return {
                "주소": address,
                "전용면적": "",
                "상태": msg
            }


        if progress:

            progress.write(
                f"② 주소 확인 완료: "
                f"{juso['roadAddr']}"
            )


        # -------------------------
        # 건축물대장
        # -------------------------

        if progress:

            progress.write(
                "③ 건축물대장 전유부 조회 중..."
            )


        platGbCd = "0"


        expos_items = get_expos_info(
            juso["sigunguCd"],
            juso["bjdongCd"],
            platGbCd,
            juso["bun"],
            juso["ji"]
        )


        # 산번지

        if not expos_items:

            platGbCd = "1"


            expos_items = get_expos_info(
                juso["sigunguCd"],
                juso["bjdongCd"],
                platGbCd,
                juso["bun"],
                juso["ji"]
            )


        if not expos_items:

            return {
                "주소": address,
                "전용면적": "",
                "상태": "건축물대장 전유부 없음"
            }



        
        if progress:

            progress.write(
                "④ 전유부 확인 완료"
            )


        # -------------------------
        # 면적
        # -------------------------

        if progress:

            progress.write(
                "⑤ 전유공용면적 조회 중..."
            )


        area_items = get_area_info(
            juso["sigunguCd"],
            juso["bjdongCd"],
            platGbCd,
            juso["bun"],
            juso["ji"],
        )
        


        
        if not area_items:

            return {
                "주소": address,
                "전용면적": "",
                "상태": "전유공용면적 조회 실패 - 로그 확인"
            }
        


        area, status, pk = find_exclusive_area(
            expos_items,
            area_items,
            target_dong,
            target_ho
        )


        if area is None:

            return {
                "주소": address,
                "전용면적": "",
                "상태": status
            }


        return {
            "주소": address,
            "전용면적": f"{area:.2f}㎡",
            "상태": "조회 성공"
        }


    except Exception as e:

        return {
            "주소": address,
            "전용면적": "",
            "상태": str(e)
        }


# ============================================================
# 10. 화면
# ============================================================

st.title("🏠 건축물 전용면적 조회")

st.write(
    "주소를 최대 10개 입력하면 "
    "건축물대장 기준 전용면적을 조회합니다."
)


st.info(
    "예: 서울특별시 관악구 신림로23길 16 "
    "일성트루엘 715"
)


# ============================================================
# 11. 주소 입력 10개
# ============================================================

addresses = []


for i in range(10):

    address = st.text_input(
        f"주소 {i + 1}",
        key=f"address_{i}",
        placeholder="도로명주소 + 건물명 + 동/호수"
    )

    addresses.append(address.strip())


# ============================================================
# 12. 조회 버튼
# ============================================================

if st.button(
    "🔎 전용면적 조회",
    type="primary",
    use_container_width=True
):

    targets = [
        x for x in addresses
        if x
    ]


    if not targets:

        st.warning(
            "주소를 하나 이상 입력해주세요."
        )

        st.stop()


    if len(targets) > 10:

        st.error(
            "최대 10개까지 조회할 수 있습니다."
        )

        st.stop()


    st.markdown("---")

    st.subheader(
        f"조회 중... {len(targets)}개 주소"
    )


    results = []


    for idx, address in enumerate(
        targets,
        start=1
    ):

        st.markdown(
            f"### {idx}. {address}"
        )


        progress_box = st.empty()


        result = process_address(
            address,
            progress_box
        )


        results.append(result)


        if result["전용면적"]:

            st.success(
                f"전용면적: "
                f"{result['전용면적']}"
            )

        else:

            st.error(
                f"조회 실패: "
                f"{result['상태']}"
            )


        time.sleep(0.2)


    # ========================================================
    # 결과표
    # ========================================================

    st.markdown("---")

    st.subheader("📋 조회 결과")


    st.dataframe(
        results,
        use_container_width=True,
        hide_index=True
    )


    # ========================================================
    # CSV 다운로드
    # ========================================================

    import pandas as pd


    result_df = pd.DataFrame(results)


    csv_data = result_df.to_csv(
        index=False,
        encoding="utf-8-sig"
    )


    st.download_button(
        "📥 결과 CSV 다운로드",
        data=csv_data,
        file_name="전용면적_조회결과.csv",
        mime="text/csv",
        use_container_width=True
        )
