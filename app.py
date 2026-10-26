import re, time, requests, urllib.parse, pandas as pd, streamlit as st
import threading, os, json, uuid, glob
from datetime import datetime

st.set_page_config(page_title="전용면적 조회", page_icon="🏠", layout="wide")
BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"

_req = ["BUILDING_API_KEY", "PERMIT_API_KEY", "JUSO_API_KEY"]
_miss = [k for k in _req if k not in st.secrets]
if _miss:
    st.error(f"⚠️ Streamlit Secrets 누락: {', '.join(_miss)}")
    st.stop()

BUILDING_API_KEY_DECODED = urllib.parse.unquote(st.secrets["BUILDING_API_KEY"])
PERMIT_API_KEY_DECODED = urllib.parse.unquote(st.secrets["PERMIT_API_KEY"])
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

APT_KW = ("자이","푸르지오","래미안","힐스테이트","아이파크","e편한세상","편한세상","더샵","롯데캐슬","SK뷰","한신더휴","한신","금호어울림","코오롱하늘채","호반베르디움","반도유보라","부영사랑으로","중흥S클래스","제일풍경채","대방노블랜드","우미린","벽산블루밍","동원로얄듀크","KCC스위첸","한화포레나","포레나","두산위브","현대홈타운","아파트","APT","포레온","올림픽파크","센트라스","파크")
VILLA_KW = ("빌라","타운하우스","빌","홈타운")

# ============================================================
# 슬롯 유틸
# ============================================================
def get_next_empty_slot():
    for i in range(MAX_ADDRESS):
        if not str(st.session_state.get(f"address_{i}", "") or "").strip():
            return i + 1
    return MAX_ADDRESS

def fill_slot(i0, v):
    st.session_state[f"address_{i0}"] = v
    if i0 + 1 > st.session_state.address_count:
        st.session_state.address_count = i0 + 1

def slot_label(s1, ns1):
    is_next = (s1 == ns1)
    has = bool(str(st.session_state.get(f"address_{s1-1}", "") or "").strip())
    if is_next and has: return f"⭐ 주소 {s1} (다음 빈슬롯 · 덮어쓰기)", True
    if is_next: return f"⭐ 주소 {s1} (다음 빈슬롯)", True
    if has: return f"주소 {s1} ✏️ (덮어쓰기)", False
    return f"주소 {s1}", False

def combine_addr(b, d):
    b = str(b or "").strip(); d = str(d or "").strip()
    return b if not d else f"{b} {d}"

@st.dialog("📥 주소 넣을 위치 선택")
def slot_picker_dialog(cand, dk, bk, ns1):
    st.caption("**선택 즉시 반영되고 창이 자동으로 닫힙니다.**")
    fv = combine_addr(cand.get(bk, ""), st.session_state.get(dk, ""))
    st.caption(f"📝 입력될 주소: `{fv}`")
    for s1 in range(1, MAX_ADDRESS + 1):
        lb, inn = slot_label(s1, ns1)
        if st.button(lb, key=f"dlg_{bk}_{s1}", use_container_width=True, type="primary" if inn else "secondary"):
            fvl = combine_addr(cand.get(bk, ""), st.session_state.get(dk, ""))
            fill_slot(s1 - 1, fvl)
            st.session_state["_flash_msg"] = f"✅ 주소 {s1}에 입력 완료: {fvl}"
            st.rerun()

# ============================================================
# 파싱
# ============================================================
def clean_addr(a):
    t = str(a).strip(); p = None
    while p != t:
        p = t; t = re.sub(r"\s*\([^()]*\)", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def extract_bldnm_parens(a):
    r = []
    for m in re.finditer(r"\(([^()]*)\)", str(a)):
        for tok in re.split(r"[,，]", m.group(1)):
            tok = tok.strip()
            if tok and not tok.endswith(("동","읍","면","리","가")):
                r.append(tok)
    return r

def norm_name(s):
    return re.sub(r"[동호제\s]", "", str(s or "").strip())

def norm_bldnm(s):
    return re.sub(r"\s+","",str(s or "")).replace("-","").replace("_","").lower()

def is_match(t, s):
    t, s = norm_name(t), norm_name(s)
    if not t or not s: return False
    if t.isdigit() and s.isdigit(): return int(t) == int(s)
    return t.upper() == s.upper()

def extract_dong_ho(a):
    t = clean_addr(a); d = h = ""
    m = re.search(r"([0-9A-Za-z]+)\s*동(?![가-힣])", t)
    if m: d = m.group(1)
    m = re.search(r"(\d+)\s*호(?![가-힣])", t)
    if m: h = m.group(1)
    if not d and not h:
        m = re.search(r"(\d{2,4})-(\d{3,4})\b", t)
        if m: d, h = m.group(1), m.group(2)
    if not h:
        tk = t.split()
        if tk and tk[-1].isdigit() and len(tk[-1]) >= 3: h = tk[-1]
    if h: h = re.sub(r"[^\d]", "", str(h))
    return d, h

def extract_road_pat(a):
    t = str(a).strip()
    m = re.search(r"([가-힣]+(?:로|길|대로)(?:\d+(?:로|길))?)\s*(\d+(?:-\d+)?)", t)
    return (m.group(1), m.group(2)) if m else (None, None)

def sanitize_road(a):
    t = clean_addr(a)
    t = re.sub(r"\s+[0-9A-Za-z]+\s*동(?![가-힣]).*", "", t)
    t = re.sub(r"\s+\d+\s*호(?![가-힣]).*", "", t)
    r, n = extract_road_pat(t)
    if r and n:
        m = re.search(re.escape(r) + r"\s*" + re.escape(n), t)
        if m: return t[:m.end()].strip()
    return t.strip()

def validate_juso(ui, ji):
    ur, un = extract_road_pat(ui)
    if not ur or not un: return True
    ja = (ji.get("roadAddrPart1","") or "") + " " + (ji.get("roadAddrPart2","") or "")
    jn = ja.replace(" ", "")
    return ur.replace(" ","") in jn and un in jn

def parse_rel_jibun(rjs):
    r = []
    if not rjs: return r
    for tok in re.split(r"[,，]", rjs):
        m = re.search(r"(\d+)(?:-(\d+))?", tok.strip())
        if m: r.append((m.group(1), m.group(2) or "0"))
    return r

# ============================================================
# Juso
# ============================================================
def search_juso_single(kw):
    p = {"confmKey": JUSO_API_KEY, "currentPage":1, "countPerPage":10, "keyword":kw, "resultType":"json", "addInfoYn":"Y"}
    d = {"keyword": kw}
    try:
        r = requests.get(JUSO_API_URL, params=p, timeout=10); d["sc"] = r.status_code
        try: data = r.json()
        except ValueError: return [], "JSON 파싱 실패", d
        c = data.get("results", {}).get("common", {})
        if c.get("errorCode","") not in ("","0"): return [], f"오류 [{c.get('errorCode')}]", d
        jl = data.get("results", {}).get("juso", [])
        return jl, "정상", d
    except requests.exceptions.RequestException as e:
        return [], f"요청 실패: {e}", d

def _juso_to_dict(j):
    ac = j.get("admCd","")
    if len(ac) < 10: return None
    b = str(int(j["lnbrMnnm"])) if j.get("lnbrMnnm","").isdigit() else "0"
    ji = str(int(j["lnbrSlno"])) if j.get("lnbrSlno","").isdigit() else "0"
    det = (j.get("detBdNmList") or "").strip()
    return {"sigunguCd": ac[:5], "bjdongCd": ac[5:10], "bun": b, "ji": ji,
            "platGbCd": "1" if str(j.get("mtYn","0"))=="1" else "0",
            "jibunAddr": j.get("jibunAddr",""), "bdNm": j.get("bdNm",""),
            "roadAddr": j.get("roadAddr",""), "is_single_building": det=="",
            "detBdNmList": det, "relJibun": j.get("relJibun","") or "",
            "buldMnnm": j.get("buldMnnm","") or "", "buldSlno": j.get("buldSlno","") or "",
            "bjdongCd_alt": j.get("naBjdongCd","") or ""}

def search_juso(a):
    ad = {}
    cl = clean_addr(a); sa = sanitize_road(a)
    cands = [("raw", a)]
    if cl and cl != a: cands.append(("cleaned", cl))
    if sa and sa not in (a, cl): cands.append(("sanitized", sa))
    lm = ""
    for lb, kw in cands:
        jl, m, d = search_juso_single(kw); ad[lb] = d; lm = m
        for j in jl:
            if not validate_juso(a, j): continue
            dd = _juso_to_dict(j)
            if dd: dd["_matched_by"] = lb; return dd, "정상", ad
    return None, f"주소 매칭 실패: {lm}", ad

def search_address_candidates(kw, cnt=20):
    p = {"confmKey": JUSO_API_KEY, "currentPage":1, "countPerPage":str(cnt), "keyword":kw, "resultType":"json", "addInfoYn":"Y"}
    try:
        r = requests.get(JUSO_API_URL, params=p, timeout=10); data = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return [], f"요청 실패: {e}"
    c = data.get("results", {}).get("common", {})
    if c.get("errorCode","") not in ("","0"): return [], f"API 오류 [{c.get('errorCode')}] {c.get('errorMessage','')}"
    jl = data.get("results", {}).get("juso", []); res = []
    for j in jl:
        fr = j.get("roadAddr",""); cr = clean_addr(fr)
        res.append({"도로명주소": fr, "도로명주소_깔끔": cr, "지번주소": j.get("jibunAddr",""),
                    "건물명": j.get("bdNm",""), "우편번호": j.get("zipNo",""), "동목록": j.get("detBdNmList","")})
    return res, None

# ============================================================
# ★ 국토부 API - dedupe 없음 + 페이지 수 기반 종료
# ============================================================
def call_api_all_pages(endpoint, sigunguCd, bjdongCd, platGbCd, bun, ji,
                       match_check=None, extra_params=None, collect_all=False):
    url = f"{BUILDING_API_BASE}/{endpoint}"
    all_items, page, error_msg = [], 1, None
    debug_snippets = []
    ROWS = 100; MAXP = 100; MAXE = 5
    ce = 0; plog = []; tot_r = 0; tp = None
    while page <= MAXP:
        p = {"serviceKey": BUILDING_API_KEY_DECODED, "sigunguCd": sigunguCd, "bjdongCd": bjdongCd,
             "platGbCd": platGbCd, "bun": str(bun).zfill(4), "ji": str(ji).zfill(4),
             "numOfRows": str(ROWS), "pageNo": str(page), "_type": "json"}
        if extra_params: p.update(extra_params)
        try:
            rt = ""
            for at in range(4):
                try:
                    r = requests.get(url, params=p, timeout=25); rt = r.text or ""
                    if rt.strip(): break
                except requests.exceptions.RequestException: rt = ""
                if at < 3: time.sleep(2.0 * (at+1))
            if not rt.strip():
                error_msg = f"[{endpoint}] page {page} 빈응답"; ce += 1
                plog.append({"page":page,"count":0,"reason":"empty"})
                if ce >= MAXE: break
                page += 1; continue
            try: data = r.json()
            except ValueError:
                error_msg = f"[{endpoint}] page {page} JSON실패"; ce += 1
                plog.append({"page":page,"count":0,"reason":"json"})
                if ce >= MAXE: break
                page += 1; continue
            h = data.get("response", {}).get("header", {})
            if h.get("resultCode","") not in ("00","0",""):
                error_msg = f"[{endpoint}] API오류 [{h.get('resultCode')}]"; break
            b = data.get("response", {}).get("body", {})
            it = b.get("items", {})
            if not it:
                ce += 1; plog.append({"page":page,"count":0,"reason":"no_items"})
                if ce >= MAXE: break
                page += 1; continue
            il = it.get("item", [])
            if isinstance(il, dict): il = [il]
            if not il:
                ce += 1; plog.append({"page":page,"count":0,"reason":"empty_list"})
                if ce >= MAXE: break
                page += 1; continue
            ce = 0
            all_items.extend(il)
            tot_r = int(b.get("totalCount",0) or 0)
            if tp is None and tot_r > 0: tp = (tot_r + ROWS - 1) // ROWS
            plog.append({"page":page,"count":len(il),"total_so_far":len(all_items),"total_reported":tot_r})
            if match_check is not None and not collect_all:
                for x in il:
                    if match_check(x):
                        debug_snippets.append({"early_match_page": page})
                        return all_items, error_msg, debug_snippets
            if tp is not None and page >= tp: break
            page += 1; time.sleep(0.3)
        except requests.exceptions.RequestException as e:
            error_msg = f"[{endpoint}] page {page} 요청실패: {e}"; ce += 1
            plog.append({"page":page,"count":0,"reason":"exc"})
            if ce >= MAXE: break
            page += 1; time.sleep(1.0)
    debug_snippets.append({"endpoint":endpoint,"pages_attempted":page-1,"total_collected":len(all_items),
                           "total_reported":tot_r,"total_pages_needed":tp,"page_log":plog})
    return all_items, error_msg, debug_snippets

# ============================================================
# 매칭 유틸
# ============================================================
def _bjdong_vars(bd, alt=None):
    v = []
    def _a(x):
        if x and x not in v: v.append(x)
    _a(bd)
    if alt: _a(alt)
    if bd and len(bd) >= 2:
        l2 = bd[-2:]
        if l2 == "00": _a(bd[:-2] + "01")
        elif l2 == "01": _a(bd[:-2] + "00")
    return v

def _norm_dig(s):
    if s is None: return ""
    s = str(s).translate(str.maketrans('０１２３４５６７８９','0123456789'))
    return re.sub(r"[\s\u00A0\u3000\u200B\u200C\u200D\uFEFF]+","",s)

def _extract_nums(s): return re.findall(r"\d+", _norm_dig(s))

def _safe_area(item):
    for f in ("area","exposArea","전유면적","exposPubuseArea","areaVal","exclusiveArea","전유면적(㎡)","areaExposPubuse","전유면적값","면적"):
        raw = item.get(f)
        if raw is None or raw == "": continue
        try:
            c = re.sub(r"[^\d.]","",str(raw).replace(",",""))
            if not c: continue
            v = float(c)
            if 1.0 <= v <= 5000.0: return round(v,2)
        except (ValueError, TypeError): continue
    return None

def _is_excl(item):
    cd = str(item.get("exposPubuseGbCd","")).strip()
    nm = str(item.get("exposPubuseGbCdNm","")).strip()
    if nm and "공용" in nm: return False
    if nm and "전유" in nm: return True
    if cd in ("1","01","001"): return True
    if cd in ("2","02","002"): return False
    h = str(item.get("hoNm","")).strip()
    if h and re.search(r"\d+",h) and not re.search(r"주차|기계|계단|홀|화장실|창고|관리|경비|지하|옥탑|부속",h): return True
    return False

def _dong_match(td, id_):
    t = str(td or "").strip(); d = str(id_ or "").strip()
    if not t or not d: return True
    if is_match(t,d): return True
    if re.match(r"^\d+\s*(?:층|F|f|FLR|flr|th|st|nd|rd)$", d): return True
    return False

def _ho_match(ht, th):
    h = str(ht or "").strip(); t = str(th or "").strip()
    if not h or not t: return False
    hn = _norm_dig(h); td = re.sub(r"[^\d]","",_norm_dig(t))
    if not td: return False
    try: ti = int(td)
    except ValueError: return False
    tv = {td, f"{td}호", f"제{td}", f"제{td}호"}
    if hn in tv: return True
    for n in re.findall(r"\d+", hn):
        try:
            if int(n) == ti: return True
        except ValueError: continue
    return False

# ============================================================
# find_dedicated_area
# ============================================================
def find_dedicated_area(sigunguCd, bjdongCd, bun, ji, platGbCd, target_dong, target_ho, juso=None, expected_bldNm=None):
    if not target_ho: return None, "호수 미인식", [], [], None
    if not target_dong: return None, "동 미입력", [], [], None
    eb = expected_bldNm or []
    errs, dbgs = [], []
    ths = str(target_ho).strip(); tds = str(target_dong).strip()

    def _bld_ok(it):
        if not eb: return True
        ib = norm_bldnm(it.get("bldNm") or "")
        if not ib: return True
        for e in eb:
            en = norm_bldnm(e)
            if en and (en == ib or en in ib or ib in en): return True
        return False

    def _try(items, relaxed_dong):
        for it in items:
            h = (it.get("hoNm") or "").strip()
            if not _ho_match(h, ths): continue
            d = (it.get("dongNm") or "").strip()
            if not relaxed_dong:
                if not d or not is_match(tds, d): continue
            else:
                if d and not _dong_match(tds, d): continue
            if not _is_excl(it): continue
            if not _bld_ok(it): continue
            v = _safe_area(it)
            if v is not None: return (v, it)
        return None

    bdv = _bjdong_vars(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    bjc = [(bun, ji)]
    if ji != "0": bjc.append((bun, "0"))
    sb = set(); bju = []
    for b, j in bjc:
        k = (str(b), str(j))
        if k not in sb: sb.add(k); bju.append((b, j))
    pcs = [platGbCd] + [x for x in ("0","1","2") if x != platGbCd]

    combos = []
    def _add(pg, bd, b, j):
        k = (str(pg), str(bd), str(b), str(j))
        if k not in combos: combos.append(k)
    for bd in bdv:
        for b, j in bju: _add(platGbCd, bd, b, j)
    for pg in pcs:
        if pg == platGbCd: continue
        for bd in bdv:
            for b, j in bju: _add(pg, bd, b, j)

    # 1단계: 전체 수집
    all_items = []
    for pg, bd, b, j in combos:
        it, er, dg = call_api_all_pages("getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j, collect_all=True)
        all_items.extend(it); dbgs.extend(dg)
        if er: errs.append(er)

    if not all_items:
        return None, "전유부 데이터 자체 없음", errs, dbgs, None

    m = _try(all_items, False)
    if m: return m[0], f"엄격동(수집{len(all_items)})", errs, dbgs, m[1]
    m = _try(all_items, True)
    if m: return m[0], f"관대동(수집{len(all_items)})", errs, dbgs, m[1]

    # 2단계: 서버 필터 fallback
    for pg, bd, b, j in combos:
        for flt in [{"hoNm": ths}, {"hoNm": f"{ths}호"}]:
            it, er, dg = call_api_all_pages("getBrExposPubuseAreaInfo", sigunguCd, bd, pg, b, j, extra_params=flt, collect_all=True)
            dbgs.extend(dg)
            if er: errs.append(er)
            if not it: continue
            mt = [x for x in it if _ho_match(x.get("hoNm",""), ths)]
            if not mt: continue
            m = _try(mt, True)
            if m: return m[0], f"서버필터({flt.get('hoNm')})-{len(mt)}건", errs, dbgs, m[1]

    # 진단
    tot_r = 0
    for d in dbgs:
        if isinstance(d, dict) and "total_reported" in d: tot_r = max(tot_r, d["total_reported"])
    strict = [x for x in all_items if str(x.get("dongNm","")).strip() and is_match(tds, x.get("dongNm"))]
    td_ = re.sub(r"[^\d]","",_norm_dig(ths))
    def _contains(h):
        if not td_: return False
        try: ti = int(td_)
        except ValueError: return False
        for n in _extract_nums(h):
            try:
                if int(n) == ti: return True
            except ValueError: pass
        return False
    strict_hits = [x for x in strict if _contains(x.get("hoNm",""))]
    all_hits = [x for x in all_items if _contains(x.get("hoNm",""))]
    msg = f"[{tds}동 {ths}호] 수집 {len(all_items)}/{tot_r}건 | {tds}동 {len(strict)}건"
    if strict_hits:
        c = strict_hits[0]; rs = []
        if not _is_excl(c): rs.append(f"공용")
        if not _bld_ok(c): rs.append(f"건물명(bldNm={c.get('bldNm')})")
        if _safe_area(c) is None: rs.append(f"면적없음(area={c.get('area')})")
        msg += f" | hoNm='{c.get('hoNm')}' 있음"
        if rs: msg += f" / 실패: {' / '.join(rs)}"
    elif all_hits:
        oth = [x for x in all_hits if not is_match(tds, x.get("dongNm"))]
        if oth:
            ds = sorted({(x.get("dongNm") or "").strip() for x in oth if (x.get("dongNm") or "").strip()})
            hs = [(x.get("hoNm") or "").strip() for x in oth[:3]]
            msg += f" | ⚠️ 다른동에만: dongNm={ds[:5]}, hoNm={hs}"
    else:
        ad = sorted({(x.get("dongNm") or "").strip() for x in all_items if (x.get("dongNm") or "").strip()})
        msg += f" | 전체에 없음 | 동: {ad[:10]}"
        an = set()
        for x in all_items: an.update(_extract_nums(x.get("hoNm","")))
        sim = sorted([n for n in an if len(n) >= 3 and len(td_) >= 2 and (n.startswith(td_[:2]) or n.endswith(td_[-2:]))])[:15]
        if sim: msg += f" | 유사: {sim}"
    return None, msg, errs, dbgs, None

# ============================================================
# 총괄표제부
# ============================================================
def _fetch_recap(sigunguCd, bjdongCd, bun, ji, platGbCd, juso=None):
    bdv = _bjdong_vars(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    pcs = [platGbCd] + [x for x in ("0","1","2") if x != platGbCd]
    dg = []
    for bd in bdv:
        for pg in pcs:
            it, er, d = call_api_all_pages("getBrRecapTitleInfo", sigunguCd, bd, pg, bun, ji, collect_all=True)
            dg.extend(d)
            if it:
                x = it[0]
                return {"지상층수": x.get("grndFlrCnt","") or "", "세대수": x.get("hhldCnt","") or x.get("hhlDcnt","") or "",
                        "호수": x.get("hoCnt","") or "", "가구수": x.get("fmlyCnt","") or "",
                        "연면적": x.get("totArea","") or "", "대지면적": x.get("platArea","") or "", "건축면적": x.get("archArea","") or ""}, dg
    return None, dg

# ============================================================
# 인허가
# ============================================================
def fetch_permit(sigunguCd, bjdongCd, bun, ji, platGbCd):
    url = f"{BUILDING_API_BASE}/getBrHstpInfo"
    bdv = _bjdong_vars(bjdongCd); pcs = [platGbCd] + [x for x in ("0","1","2") if x != platGbCd]
    dg = []
    for bd in bdv:
        for pg in pcs:
            p = {"serviceKey": PERMIT_API_KEY_DECODED, "sigunguCd": sigunguCd, "bjdongCd": bd, "platGbCd": pg,
                 "bun": str(bun).zfill(4), "ji": str(ji).zfill(4), "numOfRows": "100", "pageNo": "1", "_type": "json"}
            try:
                r = requests.get(url, params=p, timeout=15)
                if not r.text.strip(): continue
                data = r.json()
                h = data.get("response", {}).get("header", {})
                if h.get("resultCode","") not in ("00","0",""): continue
                b = data.get("response", {}).get("body", {}); it = b.get("items", {})
                if not it: continue
                il = it.get("item", [])
                if isinstance(il, dict): il = [il]
                if not il: continue
                il.sort(key=lambda x: str(x.get("crtnDay","")), reverse=True)
                x = il[0]
                return {"주택유형코드": x.get("hstpGbCd","") or "", "주택유형코드명": x.get("hstpGbCdNm","") or "", "건물명": x.get("bldNm","") or ""}, None, dg
            except (requests.exceptions.RequestException, ValueError): continue
    return None, "정보 없음", dg

def norm_hstp(cn, cd=""):
    n = str(cn or "").strip(); c = str(cd or "").strip()
    if "도시형생활주택" in n: return "도시형생활주택"
    if "아파트" in n: return "아파트"
    if "다세대" in n: return "다세대주택"
    if "연립" in n: return "연립주택"
    if "단독" in n and "다가구" in n: return "단독(다가구)주택"
    if "단독" in n: return "단독주택"
    if "다가구" in n: return "단독(다가구)주택"
    if "오피스텔" in n: return "오피스텔(업무시설)"
    if "공동주택" in n: return "공동주택"
    if c == "1": return "단독주택"
    if c == "2": return "오피스텔(업무시설)"
    if c == "3": return "공동주택"
    return n or ""

# ============================================================
# 표제부
# ============================================================
def fetch_building_type(sigunguCd, bjdongCd, bun, ji, platGbCd, juso=None, target_dong="", target_ho=""):
    bdv = _bjdong_vars(bjdongCd, juso.get("bjdongCd_alt") if juso else None)
    pcs = [platGbCd] + [x for x in ("0","1","2") if x != platGbCd]
    dg = []; ac = []
    for bd in bdv:
        for pg in pcs:
            it, er, d = call_api_all_pages("getBrTitleInfo", sigunguCd, bd, pg, bun, ji, collect_all=True)
            dg.extend(d)
            if it: ac.extend(it)
        if ac: break
    if not ac: return None, "표제부 정보 없음", dg
    x = ac[0]
    info = {"주용도코드": x.get("mainPurpsCd","") or "", "주용도": x.get("mainPurpsCdNm","") or "",
            "기타용도": x.get("etcPurps","") or "", "세대수": x.get("hhldCnt","") or x.get("hhlDcnt","") or "",
            "호수": x.get("hoCnt","") or "", "건물명": x.get("bldNm","") or "", "동명칭": x.get("dongNm","") or "",
            "사용승인일": x.get("useAprDay","") or "", "연면적": x.get("totArea","") or "",
            "대지면적": x.get("platArea","") or "", "건축면적": x.get("archArea","") or "", "지상층수": x.get("grndFlrCnt","") or ""}
    nf = (not info["지상층수"] or not info["세대수"] or not info["대지면적"] or str(info["대지면적"]).strip() in ("0","0.0","0.00"))
    if nf:
        rc, rd = _fetch_recap(sigunguCd, bjdongCd, bun, ji, platGbCd, juso)
        dg.extend(rd)
        if rc:
            for k, v in rc.items():
                cur = info.get(k, "")
                if not cur or str(cur).strip() in ("0","0.0","0.00"): info[k] = v
    return info, None, dg

def classify_housing_type(purps_cd="", purps_nm="", etc_purps="", bld_nm="", hhld="", grnd_flr="", plat_area="", arch_area="", ho_cnt="", tot_area=""):
    c = str(purps_cd or "").strip(); c = c.zfill(5) if c.isdigit() else c
    n = str(purps_nm or "").strip(); e = str(etc_purps or "").strip(); b = str(bld_nm or "").strip()
    if "도시형생활주택" in n or "도시형생활주택" in e or "도시형생활주택" in b: return "도시형생활주택"
    if c == "02001": return "아파트"
    if c == "02002": return "연립주택"
    if c == "02003": return "다세대주택"
    if c == "01003": return "단독(다가구)주택"
    if c in ("01000","01001"):
        if "다가구" in n or "다가구" in e: return "단독(다가구)주택"
        return "단독주택"
    if c == "14000":
        if "오피스텔" in n or "오피스텔" in e or "오피스텔" in b: return "오피스텔(업무시설)"
        return n or "업무시설"
    if "아파트" in n: return "아파트"
    if "다세대" in n: return "다세대주택"
    if "연립" in n: return "연립주택"
    if "다가구" in n: return "단독(다가구)주택"
    if "단독" in n: return "단독주택"
    if "오피스텔" in n: return "오피스텔(업무시설)"
    if c.startswith("02000") or "공동주택" in n or "공동주택" in e:
        ha = any(k in b for k in APT_KW); hv = any(k in b for k in VILLA_KW)
        if ha and not hv: return "아파트"
        if hv and not ha: return "다세대주택" if any(k in b for k in ("빌라","빌")) else "연립주택"
        return "공동주택"
    if "아파트" in e: return "아파트"
    if "다세대" in e: return "다세대주택"
    if "연립" in e: return "연립주택"
    if "다가구" in e: return "단독(다가구)주택"
    if "단독" in e: return "단독주택"
    if any(k in f"{n} {e}" for k in ("근린생활","상가","판매","숙박","공장","업무시설")):
        if any(k in b for k in APT_KW): return "아파트(주상복합 추정)"
        return n or "비주거용"
    return n or "미분류"

def _spec(t):
    if not t: return 0
    if "미분류" in t or "정보부족" in t: return 1
    if t == "공동주택": return 2
    if "추정" in t: return 5
    return 10

# ============================================================
# process
# ============================================================
def process_address(address, progress=None):
    if not address:
        return {"주소":"","전용면적":"","대지면적":"","주택유형":"","유형출처":"","상태":"주소 없음"}, None
    td, th = extract_dong_ho(address)
    juso, msg, jd = search_juso(address)
    if not juso:
        return {"주소":address,"전용면적":"","대지면적":"","주택유형":"","유형출처":"","상태":f"주소 검색 실패: {msg}"}, {"juso_debug":jd}
    eb = []
    if juso.get("bdNm"): eb.append(juso["bdNm"])
    for n in extract_bldnm_parens(address):
        if n not in eb: eb.append(n)
    pi, _, _ = fetch_permit(juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"], juso["platGbCd"])
    ti, _, _ = fetch_building_type(juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"], juso["platGbCd"], juso=juso, target_dong=td, target_ho=th)
    tft = ""
    if ti:
        bn = ti.get("건물명","") or (pi.get("건물명","") if pi else "") or (juso.get("bdNm","") if juso else "")
        tft = classify_housing_type(purps_cd=ti.get("주용도코드",""), purps_nm=ti.get("주용도",""), etc_purps=ti.get("기타용도",""), bld_nm=bn, hhld=ti.get("세대수",""), grnd_flr=ti.get("지상층수",""), plat_area=ti.get("대지면적",""), arch_area=ti.get("건축면적",""), ho_cnt=ti.get("호수",""), tot_area=ti.get("연면적",""))
    tfp = ""
    if pi and pi.get("주택유형코드명"): tfp = norm_hstp(pi["주택유형코드명"], pi.get("주택유형코드",""))
    if tft in ("단독주택","단독(다가구)주택") and ti:
        tt = ti.get("연면적",""); pl = ti.get("대지면적","")
        ts = ps = ""
        try:
            if tt and str(tt).strip() not in ("0","0.0","0.00"):
                v = float(str(tt).replace(",","")); ts = f"{v:.2f}㎡ ({round(v*0.3025)}평) (연면적)"
        except (ValueError, TypeError): pass
        try:
            if pl and str(pl).strip() not in ("0","0.0","0.00"): ps = f"{float(str(pl).replace(',','')):.2f}㎡"
        except (ValueError, TypeError): pass
        return {"주소":address,"전용면적":ts,"대지면적":ps,"주택유형":tft,"유형출처":"표제부기반","상태":"단독주택 연면적 반환"}, {}
    area, status, errs, ad, mu = find_dedicated_area(juso["sigunguCd"], juso["bjdongCd"], juso["bun"], juso["ji"], juso["platGbCd"], td, th, juso=juso, expected_bldNm=eb)
    tfu = ""
    if mu:
        unm = str(mu.get("mainPurpsCdNm","") or "").strip(); ucd = str(mu.get("mainPurpsCd","") or "").strip()
        if unm or ucd:
            bnu = mu.get("bldNm","") or (ti.get("건물명","") if ti else "") or (pi.get("건물명","") if pi else "") or (juso.get("bdNm","") if juso else "")
            tfu = classify_housing_type(purps_cd=ucd, purps_nm=unm, bld_nm=bnu, hhld=ti.get("세대수","") if ti else "", grnd_flr=ti.get("지상층수","") if ti else "")
    if tfu == "다세대주택" and (tft == "도시형생활주택" or tfp == "도시형생활주택"): tfu = ""
    tc = [(tfu,"전유부기반"),(tft,"표제부기반"),(tfp,"인허가API")]
    ht = ""; hs = ""; bs = 0
    for t, s in tc:
        if not t: continue
        sc = _spec(t)
        if sc > bs: ht = t; hs = s; bs = sc
    if not ht:
        for t, s in tc:
            if t: ht = t; hs = s; break
    if area:
        return {"주소":address,"전용면적":f"{area:.2f}㎡ ({round(area*0.3025)}평)","대지면적":"","주택유형":ht or "-","유형출처":hs,"상태":status}, {"api_debug":ad}
    if ti and ti.get("연면적"):
        try:
            tv = float(str(ti["연면적"]).replace(",",""))
            return {"주소":address,"전용면적":f"{tv:.2f}㎡ ({round(tv*0.3025)}평) (연면적 폴백)","대지면적":"","주택유형":ht or "-","유형출처":hs,"상태":f"전유부 조회 실패 ({status})"}, {"api_debug":ad}
        except (ValueError, TypeError): pass
    return {"주소":address,"전용면적":"","대지면적":"","주택유형":ht or "-","유형출처":hs,"상태":status}, {"api_debug":ad}

# ============================================================
# 백그라운드
# ============================================================
def _bg_query(qid, addrs, uid):
    sp = os.path.join(QUERY_DIR, f"{uid}_{qid}_status.json")
    rp = os.path.join(QUERY_DIR, f"{uid}_{qid}_results.json")
    dp = os.path.join(QUERY_DIR, f"{uid}_{qid}_debug.json")
    try:
        rs = []; db = []
        for i, a in enumerate(addrs, 1):
            try:
                with open(sp, "w", encoding="utf-8") as f:
                    json.dump({"status":"running","current":i,"total":len(addrs),"address":a}, f, ensure_ascii=False)
            except IOError: pass
            try:
                r, d = process_address(a, progress=None); db.append({"주소":a,"debug":d})
            except Exception as e:
                r = {"주소":a,"전용면적":"","대지면적":"","주택유형":"","유형출처":"","상태":f"오류: {e}"}
            rs.append(r)
        with open(rp, "w", encoding="utf-8") as f: json.dump(rs, f, ensure_ascii=False)
        with open(dp, "w", encoding="utf-8") as f: json.dump(db, f, ensure_ascii=False)
        with open(sp, "w", encoding="utf-8") as f: json.dump({"status":"done","total":len(addrs)}, f, ensure_ascii=False)
    except Exception as e:
        try:
            with open(sp, "w", encoding="utf-8") as f: json.dump({"status":"error","error":str(e)}, f, ensure_ascii=False)
        except IOError: pass

def render_results(rs):
    succ = sum(1 for r in rs if r.get("전용면적") or (r.get("주택유형") and r.get("주택유형") != "-"))
    err = len(rs) - succ
    if err == 0: bg = "linear-gradient(90deg,#28a745,#34ce57)"; m = f"✅ {len(rs)}건 조회 완료"
    elif succ == 0: bg = "linear-gradient(90deg,#dc3545,#e4606d)"; m = f"⚠️ {len(rs)}건 조회 오류"
    else: bg = "linear-gradient(90deg,#17a2b8,#48c6dc)"; m = f"✅ {succ}건 완료 | ⚠️ {err}건 오류"
    st.markdown(f"<div style='background:{bg};color:#fff;padding:16px 22px;border-radius:10px;font-weight:bold;font-size:1.15rem;text-align:center;'>{m}</div>", unsafe_allow_html=True)
    for i, r in enumerate(rs, 1):
        st.markdown(f"### {i}. {r.get('주소','')}")
        tt = r.get("주택유형") or "-"; at = r.get("전용면적") or "-"; pt = r.get("대지면적") or ""
        stt = r.get("상태",""); src = r.get("유형출처",""); b = f" [{src}]" if src else ""
        if pt: st.success(f"🏢 주택유형: **{tt}**{b} | 📐 연면적: {at} | 🗺️ 대지면적: {pt} ({stt})")
        elif at and at != "-": st.success(f"🏢 주택유형: **{tt}**{b} | 📐 면적: {at} ({stt})")
        else:
            if tt and tt != "-": st.info(f"🏢 주택유형: **{tt}**{b}")
            st.error(f"조회 실패: {stt}")
    st.markdown("---"); st.subheader("📋 조회 결과")
    df = pd.DataFrame(rs)
    for c in ["주소","전용면적","대지면적","주택유형","유형출처","상태"]:
        if c not in df.columns: df[c] = ""
    df = df[["주소","전용면적","대지면적","주택유형","유형출처","상태"]]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("📥 CSV", data=df.to_csv(index=False, encoding="utf-8-sig"), file_name="결과.csv", mime="text/csv", use_container_width=True)

@st.fragment(run_every=1.5)
def render_query_status():
    qid = st.session_state.get("_current_query_id")
    if not qid: return
    ck = f"_results_cache_{qid}"
    if ck in st.session_state: render_results(st.session_state[ck]); return
    sp = os.path.join(QUERY_DIR, f"{USER_ID}_{qid}_status.json")
    rp = os.path.join(QUERY_DIR, f"{USER_ID}_{qid}_results.json")
    if not os.path.exists(sp): st.info("⏳ 준비 중..."); return
    try:
        with open(sp, encoding="utf-8") as f: s = json.load(f)
    except (json.JSONDecodeError, IOError): return
    if s["status"] == "running":
        st.markdown(f"<div style='background:linear-gradient(90deg,#ff9a00,#ff6a00);color:#fff;padding:14px 20px;border-radius:10px;font-weight:bold;font-size:1.15rem;'>⏳ 조회 중... {s['current']} / {s['total']} 건<span style='display:block;margin-top:6px;font-weight:normal;font-size:0.92rem;'>📍 {s['address']}</span></div>", unsafe_allow_html=True)
    elif s["status"] == "done":
        try:
            with open(rp, encoding="utf-8") as f: rs = json.load(f)
            st.session_state[ck] = rs; render_results(rs)
        except (json.JSONDecodeError, IOError): st.error("결과 읽기 실패")
    elif s["status"] == "error": st.error(f"오류: {s.get('error','알 수 없음')}")

# ============================================================
# UI
# ============================================================
st.title("🏠 건축물 전용면적 · 주택유형 조회")

if "show_recent" not in st.session_state: st.session_state.show_recent = False
rf_list = sorted(glob.glob(os.path.join(QUERY_DIR, "*_results.json")), key=os.path.getmtime, reverse=True)[:5]
if rf_list:
    st.markdown("<style>div.recent-toggle .stButton>button{background:transparent!important;border:none!important;color:#b8b8b8!important;font-size:0.78rem!important;font-weight:400!important;padding:0!important;margin:0!important;min-height:0!important;line-height:1.3!important;text-align:left!important;box-shadow:none!important;justify-content:flex-start!important;}div.recent-toggle .stButton>button:hover{color:#777!important;}</style>", unsafe_allow_html=True)
    st.markdown('<div class="recent-toggle">', unsafe_allow_html=True)
    ar = "▾" if st.session_state.show_recent else "▸"
    if st.button(f"{ar} 최근 조회 결과 {len(rf_list)}건", key="tg_recent"): st.session_state.show_recent = not st.session_state.show_recent; st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    if st.session_state.show_recent:
        with st.container(border=True):
            for rf in rf_list:
                try:
                    mt = datetime.fromtimestamp(os.path.getmtime(rf)).strftime("%m-%d %H:%M")
                    with open(rf, encoding="utf-8") as fh: dt = json.load(fh)
                    fk = os.path.basename(rf).replace("_results.json","")
                    c1, c2 = st.columns([5,1])
                    with c1: st.caption(f"[{mt}] {len(dt)}건")
                    with c2:
                        if st.button("불러오기", key=f"ld_{fk}", use_container_width=True):
                            st.session_state["_current_query_id"] = fk; st.session_state[f"_results_cache_{fk}"] = dt; st.rerun()
                except (json.JSONDecodeError, IOError): pass

with st.expander("🔍 도로명주소 / 건물명으로 먼저 검색", expanded=False):
    for k in ["search_candidates","search_error","search_performed","show_search_results"]:
        if k not in st.session_state: st.session_state[k] = [] if k=="search_candidates" else (None if k=="search_error" else (False if k=="search_performed" else True))
    c1, c2 = st.columns([4,1])
    with c1: kw = st.text_input("검색어", key="juso_search_kw", placeholder="예: 반포자이 / 관문로 106", label_visibility="collapsed")
    with c2: ds = st.button("🔍 검색", use_container_width=True)
    if ds:
        if not kw.strip(): st.warning("검색어 입력")
        else:
            with st.spinner("검색 중..."):
                cd, er = search_address_candidates(kw.strip(), count=20)
            st.session_state.search_candidates = cd; st.session_state.search_error = er; st.session_state.search_performed = True; st.session_state.show_search_results = True
    if st.session_state.search_performed:
        er = st.session_state.search_error; cd = st.session_state.search_candidates
        if er: st.error(f"검색 실패: {er}")
        elif not cd: st.info("결과 없음")
        else:
            hc1, hc2 = st.columns([4,1])
            with hc1: st.markdown(f"### 🔍 총 {len(cd)}건")
            with hc2:
                tl = "🔼 접기" if st.session_state.show_search_results else "🔽 펼치기"
                if st.button(tl, use_container_width=True, key="tg_search"):
                    st.session_state.show_search_results = not st.session_state.show_search_results; st.rerun()
            if st.session_state.show_search_results:
                ns1 = get_next_empty_slot()
                st.info(f"**⭐ 주소 {ns1}** 기본 선택")
                for i, c in enumerate(cd, start=1):
                    with st.container(border=True):
                        cl1, cl2 = st.columns([5,1])
                        with cl1: st.markdown(f"**{i}. {c['도로명주소']}**  `{c['우편번호']}`")
                        with cl2:
                            if st.button("🔽 접기", key=f"cf_{i}", use_container_width=True):
                                st.session_state.show_search_results = False; st.rerun()
                        dk = f"detail_{i}"
                        with st.container(border=True):
                            st.markdown("##### 📝 상세주소 (동/호수)")
                            dv = st.text_input("상세주소", key=dk, placeholder="예: 113동 401호", label_visibility="collapsed")
                            st.markdown(f"📄 원본: `{combine_addr(c['도로명주소'], dv)}`  \n📄 괄호제거: `{combine_addr(c['도로명주소_깔끔'], dv)}`")
                        if c["건물명"]: st.caption(f"🏢 {c['건물명']}")
                        if c["지번주소"]: st.caption(f"📍 {c['지번주소']}")
                        b1, b2 = st.columns(2)
                        with b1:
                            if st.button("📥 원본 주소 넣기", key=f"o_{i}", use_container_width=True):
                                st.session_state["_picker_open"] = {"candidate":c,"detail_key":dk,"base_key":"도로명주소","next_slot":ns1}; st.rerun()
                        with b2:
                            if st.button("📥 괄호 제거 주소 넣기", key=f"c_{i}", use_container_width=True):
                                st.session_state["_picker_open"] = {"candidate":c,"detail_key":dk,"base_key":"도로명주소_깔끔","next_slot":ns1}; st.rerun()

st.markdown("---")
_flash = st.session_state.pop("_flash_msg", None)
if _flash: st.success(_flash)

st.subheader("📋 전용면적 · 주택유형 조회")
st.caption("💡 'OO동 OOOO호' 형식 권장. 탭 닫아도 서버 살아있으면 계속.")

addrs = []
for i in range(st.session_state.address_count):
    a = st.text_input(f"주소 {i+1}", key=f"address_{i}", placeholder="예: 서울특별시 성동구 왕십리로 410 센트라스 113동 401호")
    addrs.append(a.strip())

c1, c2, c3 = st.columns([1,1,2])
with c1:
    if st.button("➕ 추가", disabled=(st.session_state.address_count >= MAX_ADDRESS), use_container_width=True, key="ba"):
        st.session_state.address_count += 1; st.rerun()
with c2:
    if st.button("➖ 삭제", disabled=(st.session_state.address_count <= 1), use_container_width=True, key="br"):
        st.session_state.pop(f"address_{st.session_state.address_count-1}", None); st.session_state.address_count -= 1; st.rerun()
with c3:
    dq = st.button("🔎 조회 (백그라운드)", type="primary", use_container_width=True, key="bq")

st.caption(f"입력칸: **{st.session_state.address_count} / {MAX_ADDRESS}**")
st.markdown("---")

if dq:
    tgs = [x for x in addrs if x]
    if not tgs: st.warning("주소 입력"); st.stop()
    qid = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    st.session_state["_current_query_id"] = qid
    threading.Thread(target=_bg_query, args=(qid, tgs, USER_ID), daemon=True).start()
    st.success(f"🚀 시작! ID: `{qid}`"); st.info("탭 닫아도 계속."); time.sleep(0.5); st.rerun()

render_query_status()
_p = st.session_state.pop("_picker_open", None)
if _p: slot_picker_dialog(_p["candidate"], _p["detail_key"], _p["base_key"], _p["next_slot"])
