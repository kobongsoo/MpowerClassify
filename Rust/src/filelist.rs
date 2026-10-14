//! 문서 식별자(doc_id) · 입력 목록(--filelist) 어댑터 — Python 판의 짝.
//!
//! 설계: plan/문서분류체계-연동-분류규칙-설계.html §7-5 · §7-5-2-1 · §7-5-3 (D0 · D0b)
//!
//! 이 모듈이 푸는 문제는 하나다 — "같은 문서인가"를 무엇으로 판단할 것인가.
//! 지금까지는 경로 문자열이 유일한 이름표였는데, 경로는 문서의 주소이지 신분증이
//! 아니다. 폴더를 옮기거나 이름만 바꿔도 값이 달라지고, 그러면 그 문서에 쌓아 둔
//! 사람의 판단 이력이 통째로 끊긴다. 실제로 저장소에서 드라이브 문자 대소문자
//! 차이(D: vs d:) 하나 때문에 사람이 고친 등급 4건이 조용히 반영되지 않고 있었다.
//!
//! [Python 과의 관계] `src/csoclassify/filelist.py` 와 **같은 값을 내야 한다**.
//! 두 판이 다른 doc_id 를 내면 같은 문서가 매핑 테이블에 두 건으로 들어간다.
//! `tests::python_판과_같은_정규화를_한다` 가 같은 입력 표로 그것을 지킨다.

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::Read;
use std::path::Path;

/// doc_id 폭. 문서분류체계 DB 의 DC_ID 가 VARCHAR(40) 이라 짝이 될 doc_id 도 같은
/// 폭으로 맞춘다. SHA-256 hex 는 64자라 앞 40자(160비트)만 쓴다 — 1억 문서 기준
/// 충돌 확률이 약 10⁻³³ 로 사실상 0 이고, SHA-1(정확히 40자)보다 충돌 내성이 좋다.

//------------------------------------------------------------------
// 경로 → 정규화 키
//=> 같은 파일을 가리키는 서로 다른 표기를 한 모양으로 모은다.
//    1) 구분자를 '/' 로 통일     (D:\a\b  →  D:/a/b)
//    2) '..' · '.' 을 해소       (D:/a/../b  →  D:/b)
//    3) 드라이브 문자만 대문자   (d:/a  →  D:/a)
//
//   [왜 대소문자를 보존하나] 윈도우는 경로 대소문자를 구분하지 않고 리눅스는
//   구분한다. 정규화 규칙을 OS 별로 다르게 하면 같은 결과 파일이 OS 를 옮길 때
//   다르게 해석된다. 그래서 리눅스 기준(보존)으로 고정하고, 대소문자만 다른
//   경우는 매칭 3단계에서 한 번 더 시도하되 그 사실을 기록한다.
//
//   [왜 std 의 canonicalize 를 안 쓰나] 그건 파일이 실제로 있어야 하고 심볼릭
//   링크까지 따라간다. 우리는 '목록에 적힌 글자'와 '스캔한 글자'를 맞추는 것이지
//   디스크를 묻는 것이 아니다 — 없는 파일도 키를 가져야 한다(F4).
//------------------------------------------------------------------
pub fn normalize_key(path: &str) -> String {
    let s = path.trim().replace('\\', "/");
    if s.is_empty() {
        return String::new();
    }
    // UNC(//server/share)의 앞 슬래시 두 개는 의미가 있어 따로 떼어 둔다.
    let unc = s.starts_with("//");
    let body = if unc { &s[2..] } else { &s[..] };

    // '.' 은 버리고 '..' 은 한 칸 되돌린다. 절대경로 앞의 빈 조각은 살려 둬야
    // 선행 '/' 가 사라지지 않는다.
    let lead_slash = body.starts_with('/');
    let mut out: Vec<&str> = Vec::new();
    for part in body.split('/') {
        match part {
            "" | "." => continue,
            ".." => {
                // 되돌릴 것이 없으면(맨 앞의 '..') 그대로 둔다 — 지우면 다른 경로가 된다.
                if matches!(out.last(), Some(&p) if p != "..") {
                    out.pop();
                } else {
                    out.push(part);
                }
            }
            _ => out.push(part),
        }
    }
    let mut joined = out.join("/");
    if lead_slash {
        joined.insert(0, '/');
    }
    if unc {
        joined.insert_str(0, "//");
    }
    // 드라이브 문자(맨 앞 "x:")만 대문자로. 그 뒤 내용은 손대지 않는다.
    let b: Vec<char> = joined.chars().collect();
    if b.len() >= 2 && b[1] == ':' && b[0].is_ascii_alphabetic() {
        joined = format!("{}{}", b[0].to_ascii_uppercase(),
                         b[1..].iter().collect::<String>());
    }
    joined
}

//------------------------------------------------------------------
// 파일 내용의 지문(SHA-256 전체)
//=> "이 문서가 무엇인가"를 내용으로 가리는 값. 옮기거나 이름을 바꿔도 그대로고,
//   내용을 고치면 바뀐다. 결과 레코드의 hash 칸이자, 화면이 사람의 수정 기록을
//   문서에 다시 붙일 때 쓰는 결합 키다(파이썬 filelist.content_hash 와 같은 값).
//
//   [왜 자르지 않나 — 2026-09-10] 예전에는 40자로 잘라 doc_id 에 넣었다.
//   지금은 자르지 않은 전체를 hash 칸에 그대로 싣는다.
//------------------------------------------------------------------
pub fn content_hash(path: &Path) -> Option<String> {
    let mut f = std::fs::File::open(path).ok()?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];      // 1MB 씩 — 대용량에서 메모리가 튀지 않게
    loop {
        let n = f.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    let d = h.finalize();
    let mut out = String::with_capacity(64);
    for b in d {
        out.push_str(&format!("{:02x}", b));
    }
    Some(out)
}

//------------------------------------------------------------------
// 이 줄은 주석인가
//=> 목록 파일에 "무엇을 적어야 하는지"를 사람 말로 적어 둘 수 있게, '#' 로
//   시작하는 줄을 데이터가 아닌 것으로 본다.
//
//   [왜 '#' 만으로 판정하지 않나] 경로가 '#' 로 시작할 수 있기 때문이다. 실제로
//   이 저장소 샘플에도 `#외부유출금지_회사규정#` 같은 폴더가 있다. 그래서
//   '#' **뒤에 공백이나 '#' 이 오거나 줄이 거기서 끝날 때만** 주석으로 본다.
//   `#외부유출금지#/a.docx,SF-1` 은 '#' 뒤가 한글이라 그대로 데이터로 읽힌다.
//
//   jsonl 은 줄이 '{' 로 시작하므로 애초에 충돌하지 않는다. 헷갈릴 여지가 있는
//   쪽은 csv 의 첫 칸(경로)뿐이다.
//
//   [Python 과의 관계] `filelist.py::_is_comment` 와 판정이 같아야 한다.
//   한쪽만 고치면 같은 목록이 두 판에서 다르게 읽힌다.
//------------------------------------------------------------------
pub fn is_comment(line: &str) -> bool {
    let t = line.trim_start();
    match t.strip_prefix('#') {
        // '#' 뒤가 없으면(줄 끝) 빈 주석 줄이다.
        Some(rest) => rest.is_empty()
            || rest.starts_with('#')
            || rest.starts_with(char::is_whitespace),
        None => false,
    }
}

//------------------------------------------------------------------
// 목록 한 줄 — 경로와 그 문서의 sfile_id
//------------------------------------------------------------------
#[derive(Clone, Debug)]
pub struct Entry {
    pub path: String,
    pub sfile_id: String,
    pub hash: Option<String>,
    /// 복호화 정보(mrk·enc_mode·zip_mode). 세 칸이 모두 없으면 None — 그대로 읽는 문서다.
    /// 설계: plan/엠파워v11-암호화문서-복호화연동-설계-20261012.html §3
    pub dec: Option<DecInfo>,
    /// 열쇠 칸이 '일부만' 있거나 감싼 열쇠(v2:)가 온 줄의 사유(F8). 이 줄은 분류하지
    /// 않고 오류 레코드로 남긴다 — 그대로 읽으면 암호문을 분류하게 된다.
    pub dec_problem: Option<(&'static str, String)>,
    /// 원래 파일 이름(LFILE_NAME). 저장 이름에는 확장자도 뜻도 없다.
    pub name: Option<String>,
    /// 원래 파일 크기(LFILE_SIZE). 풀린 결과와 견준다.
    pub size: Option<u64>,
}

//------------------------------------------------------------------
// 복호화에 필요한 세 값 — 목록 한 줄 또는 명령줄에서 온다
//=> removeenc.sh 의 -k · -e · -z 로 그대로 넘어간다. enc_mode·zip_mode 는 값을
//   해석하지 않고 글자 그대로 들고 다닌다(알고리즘 번호의 뜻은 엠파워가 안다).
//   다만 "둘 다 0"만은 본다 — 엠파워도 그때는 복호화하지 않고 복사만 한다.
//
//   [왜 Debug 를 직접 쓰나] derive 하면 디버그 출력·패닉 문구에 열쇠가 찍힌다.
//   열쇠는 로그에도 결과에도 남기지 않는다(설계 §8).
//------------------------------------------------------------------
#[derive(Clone)]
pub struct DecInfo {
    pub mrk: String,
    pub enc_mode: String,
    pub zip_mode: String,
}

impl std::fmt::Debug for DecInfo {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "DecInfo {{ mrk: ****, enc_mode: {}, zip_mode: {} }}", self.enc_mode, self.zip_mode)
    }
}

impl DecInfo {
    /// 암호화도 저장 시 압축도 안 된 파일인가(ENC_MODE=0 · ZIP_MODE=0).
    /// 엠파워 MCrypt::mpowerDecrypt 와 같은 판정이다 — 이때는 도구를 부르지 않는다.
    pub fn is_plain(&self) -> bool {
        is_zero(&self.enc_mode) && is_zero(&self.zip_mode)
    }
}

/// "0" · "00" · " 0 " 처럼 숫자 0 을 뜻하는 글자인가. 빈 값은 0 이 아니다.
fn is_zero(v: &str) -> bool {
    let t = v.trim();
    !t.is_empty() && t.chars().all(|c| c == '0')
}

//------------------------------------------------------------------
// 열쇠 세 칸 → 복호화 정보 (목록 한 줄 · 명령줄 공용)
//=> 세 칸의 있고 없음으로 그 문서를 어떻게 다룰지 정한다.
//    1) 셋 다 없다            → Ok(None)  : 그대로 읽는 문서(지금까지와 같다)
//    2) 모드 둘이 모두 0      → Ok(Some)  : 암호화 안 된 파일. mrk 는 없어도 된다
//    3) 셋 다 있다            → Ok(Some)  : 복호화 대상
//    4) 일부만 있다           → Err(info_incomplete)
//    5) mrk 가 'v2:' 로 시작  → Err(key_wrapped) — KEK 로 감싼 값이라 엠파워가 벗겨야 한다
//
// -in: mrk · enc_mode · zip_mode = 각 칸의 값(없으면 None, 빈 문자열도 없는 것으로 본다)
//
// -out: Ok(Option<DecInfo>) = 위 1)~3)
// -out: error = Err((사유 이름, 사람이 읽는 문장)) — 위 4)·5)
//------------------------------------------------------------------
pub fn parse_dec_info(mrk: Option<&str>, enc_mode: Option<&str>, zip_mode: Option<&str>)
    -> Result<Option<DecInfo>, (&'static str, String)> {
    let clean = |v: Option<&str>| v.map(|s| s.trim()).filter(|s| !s.is_empty()).map(|s| s.to_string());
    let (m, e, z) = (clean(mrk), clean(enc_mode), clean(zip_mode));
    if m.is_none() && e.is_none() && z.is_none() {
        return Ok(None);
    }
    let (e, z) = match (e, z) {
        (Some(e), Some(z)) => (e, z),
        (e, z) => {
            let mut lack = vec![];
            if e.is_none() { lack.push("enc_mode"); }
            if z.is_none() { lack.push("zip_mode"); }
            return Err(("info_incomplete", format!(
                "복호화 정보가 일부만 있습니다(없는 칸: {}) — mrk · enc_mode · zip_mode 는 함께 줘야 합니다",
                lack.join(", "))));
        }
    };
    let info = DecInfo { mrk: m.unwrap_or_default(), enc_mode: e, zip_mode: z };
    if info.is_plain() {
        return Ok(Some(info));
    }
    if info.mrk.is_empty() {
        return Err(("info_incomplete",
            "복호화 정보가 일부만 있습니다(없는 칸: mrk) — 암호화·압축된 파일은 열쇠가 있어야 풉니다".into()));
    }
    if info.mrk.starts_with("v2:") {
        return Err(("key_wrapped",
            "mrk 가 KEK 로 감싼 값(v2:)입니다 — 엠파워 쪽에서 MCrypt::decryptMRKWithKEK 로 벗긴 평문을 줘야 합니다".into()));
    }
    Ok(Some(info))
}

//------------------------------------------------------------------
// 읽어 들인 입력 목록 — 경로로 sfile_id 를 찾는 사전
//=> 목록은 '대상 지정'과 'ID 사전' 두 가지로 쓰인다. 어느 쪽이든 조회는 정규화
//   키로 하고, 못 찾으면 대소문자를 접어 한 번 더 본다.
//------------------------------------------------------------------
#[derive(Debug)]
pub struct FileList {
    pub entries: HashMap<String, Entry>,
    pub warnings: Vec<String>,
    /// 해석 대상이 된 데이터 줄 수(빈 줄·주석 제외).
    pub lines: usize,
    /// 주석으로 보고 건너뛴 줄 수.
    pub comments: usize,
    pub bad_lines: usize,
    pub no_id: usize,
    pub missing_file: usize,
    pub dup_sfile_id: usize,
    folded: HashMap<String, String>,
}

impl FileList {
    //--------------------------------------------------------------
    // 경로로 목록 항목 찾기
    //=> ① 정규화 키가 같으면 그것, ② 대소문자만 다르면 그것(구제).
    //--------------------------------------------------------------
    pub fn lookup(&self, path: &str) -> Option<(&Entry, &'static str)> {
        let key = normalize_key(path);
        if let Some(e) = self.entries.get(&key) {
            return Some((e, "key"));
        }
        let k2 = self.folded.get(&key.to_lowercase())?;
        self.entries.get(k2).map(|e| (e, "case"))
    }

    //--------------------------------------------------------------
    // 목록이 정한 처리 대상 경로들
    //=> --filelist 를 단독으로 준 경우에 쓴다. 실제로 있는 파일만 돌려준다
    //   (없는 파일은 F4 로 이미 경고했다).
    //--------------------------------------------------------------
    pub fn target_paths(&self) -> Vec<String> {
        let mut v: Vec<String> = self.entries.values()
            .filter(|e| Path::new(&e.path).is_file())
            .map(|e| e.path.clone())
            .collect();
        v.sort();
        v
    }

    /// 목록에 있는데 디스크에 없는 항목들 — (경로, sfile_id).
    /// F4 로 건수만 경고하던 것을 '결과에도 남기기' 위해 항목 자체를 준다.
    /// 부르는 쪽(MpowerV11)은 자기가 준 sfile_id 로 무엇이 빠졌는지 알아야
    /// 목록을 고칠 수 있다 — 경로만으로는 어느 문서인지 되짚기 어렵다.
    pub fn missing_entries(&self) -> Vec<(String, String)> {
        let mut v: Vec<(String, String)> = self.entries.values()
            .filter(|e| !Path::new(&e.path).is_file())
            .map(|e| (e.path.clone(), e.sfile_id.clone()))
            .collect();
        v.sort();
        v
    }

    /// 복호화 정보가 붙은 줄 수(둘 다 0 인 '평문' 줄과 F8 줄 포함) — 적재 안내에 쓴다.
    pub fn dec_count(&self) -> usize {
        self.entries.values().filter(|e| e.dec.is_some() || e.dec_problem.is_some()).count()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

//------------------------------------------------------------------
// 목록 파일 읽기 (jsonl · csv) — 핵심
//=> MpowerV11 이 뽑아 준 {경로, sfile_id} 목록을 읽어 색인을 만든다.
//    1) 첫 줄이 '{' 이거나 확장자가 json 계열이면 jsonl, 아니면 csv
//    2) 한 줄씩 해석하며 F1(파싱 실패)·F2(ID 없음)를 세어 건너뛴다
//    3) 같은 정규화 키에 다른 ID 가 오면 F3 으로 즉시 멈춘다
//    4) 다 읽은 뒤 F4(파일 없음)·F7(한 ID 가 여러 경로)을 경고로 모은다
//
//   [왜 F3 만 멈추나] 한 파일에 두 ID 는 모순이라 어느 쪽을 골라도 틀린다.
//   나머지는 "이만큼은 ID 를 못 얻었다"고 세어서 알려 주면 충분하다.
//------------------------------------------------------------------
pub fn load(path: &str) -> Result<FileList, String> {
    let p = Path::new(path);
    if !p.is_file() {
        return Err(format!("목록 파일을 찾을 수 없습니다: {}", path));
    }
    let raw = std::fs::read(p)
        .map_err(|e| format!("목록 파일을 읽을 수 없습니다: {} ({})", path, e))?;
    // BOM 을 떼고 읽는다 — 안 떼면 첫 열 이름이 '\u{feff}path' 가 된다.
    let text = String::from_utf8_lossy(&raw);
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text).to_string();

    let base = p.parent().map(|d| d.to_path_buf()).unwrap_or_default();
    let fname = p.file_name().map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string());

    // 물리 줄번호를 달아서 들고 다닌다 — 빈 줄과 주석을 걷어내도 오류 메시지의
    // "N번째 줄"이 사람이 편집기에서 보는 줄번호와 같아야 찾아가 고칠 수 있다.
    let numbered: Vec<(usize, &str)> =
        text.lines().enumerate().map(|(i, l)| (i + 1, l)).collect();
    let comments = numbered.iter().filter(|(_, l)| is_comment(l)).count();
    let lines: Vec<(usize, &str)> = numbered.into_iter()
        .filter(|(_, l)| !l.trim().is_empty() && !is_comment(l))
        .collect();
    if lines.is_empty() {
        // 주석만 남은 경우는 "빈 파일"과 원인이 달라 따로 알려 준다.
        return Err(if comments > 0 {
            format!("목록 {} — 주석({}줄)뿐이고 데이터 줄이 하나도 없습니다", fname, comments)
        } else {
            format!("목록 파일이 비어 있습니다: {}", path)
        });
    }

    let (rows, mut bad) = parse_rows(&lines, path)?;

    let mut entries: HashMap<String, Entry> = HashMap::new();
    let mut seen_id: HashMap<String, Vec<String>> = HashMap::new();
    let mut no_id = 0usize;

    for (lineno, row) in rows {
        let raw_path = row.get("path").map(|s| s.trim()).unwrap_or("");
        if raw_path.is_empty() {
            bad += 1;
            continue;
        }
        let sfile_id = row.get("sfile_id").map(|s| s.trim()).unwrap_or("");
        // F2 — sfile_id 가 비었거나 없으면 그 줄은 없는 것으로 본다.
        if sfile_id.is_empty() {
            no_id += 1;
            continue;
        }
        // 상대경로는 '현재 작업폴더'가 아니라 '목록 파일이 있는 폴더' 기준으로 푼다.
        // 목록과 문서를 함께 다른 곳으로 옮겨도 깨지지 않게 하려는 것이다.
        let abs = if Path::new(raw_path).is_absolute()
            || raw_path.starts_with("//") || raw_path.starts_with("\\\\") {
            raw_path.to_string()
        } else {
            base.join(raw_path).to_string_lossy().into_owned()
        };
        let key = normalize_key(&abs);
        // F3 — 같은 파일에 다른 ID. 어느 쪽을 골라도 틀리므로 멈춘다.
        if let Some(prev) = entries.get(&key) {
            if prev.sfile_id != sfile_id {
                return Err(format!(
                    "목록 {} {}번째 줄 — 같은 파일에 sfile_id 가 두 개입니다(F3).\n  \
                     경로: {}\n  먼저: {}\n  나중: {}\n  \
                     어느 쪽을 골라도 틀리므로 분류를 시작하지 않았습니다. \
                     목록을 고쳐 다시 실행하세요.",
                    fname, lineno, key, prev.sfile_id, sfile_id));
            }
        } else {
            let h = row.get("hash").map(|s| s.trim()).filter(|s| !s.is_empty())
                .map(|s| s.to_string());
            // 복호화 정보(설계 §3). 일부만 있는 줄(F8)은 건너뛰지 않고 표식만 달아 둔다 —
            // 건너뛰면 그 문서가 결과에서 사라지고, 그대로 읽으면 암호문을 분류한다.
            let (dec, dec_problem) = match parse_dec_info(
                row.get("mrk").map(|s| s.as_str()),
                row.get("enc_mode").map(|s| s.as_str()),
                row.get("zip_mode").map(|s| s.as_str())) {
                Ok(d) => (d, None),
                Err(why) => (None, Some(why)),
            };
            let name = row.get("name").map(|s| s.trim()).filter(|s| !s.is_empty())
                .map(|s| s.to_string());
            let size = row.get("size").and_then(|s| s.trim().parse::<u64>().ok());
            entries.insert(key.clone(), Entry {
                path: abs, sfile_id: sfile_id.to_string(), hash: h,
                dec, dec_problem, name, size,
            });
        }
        seen_id.entry(sfile_id.to_string()).or_default().push(key);
    }

    // F1 — 한 줄도 못 읽었으면 데이터가 아니라 형식을 잘못 준 것이다.
    if entries.is_empty() {
        return Err(format!(
            "목록 {} — 쓸 수 있는 줄이 하나도 없습니다(데이터 {}줄 · 파싱 실패 {} · \
             sfile_id 없음 {}).\n  jsonl 이면 {{\"path\": ..., \"sfile_id\": ...}} \
             한 줄씩, csv 면 첫 줄에 path,sfile_id 헤더가 필요합니다.\n  \
             ('#' 로 시작하는 줄은 주석으로 건너뜁니다)",
            fname, lines.len(), bad, no_id));
    }

    let mut warnings = Vec::new();
    // F4 — 목록에 있는데 파일이 없다. 목록이 낡았을 수 있다(경고만).
    let missing: Vec<&Entry> = entries.values()
        .filter(|e| !Path::new(&e.path).is_file()).collect();
    let missing_file = missing.len();
    if missing_file > 0 {
        warnings.push(format!(
            "[F4] 목록에 있으나 파일이 없습니다: {}건 (예: {}) — 목록이 낡았을 수 있습니다",
            missing_file, missing[0].path));
    }
    // F7 — 같은 ID 가 여러 경로에. 사본이면 정상이라 경고만 한다.
    let dups: Vec<(&String, &Vec<String>)> =
        seen_id.iter().filter(|(_, v)| v.len() > 1).collect();
    let dup_sfile_id = dups.len();
    if dup_sfile_id > 0 {
        warnings.push(format!(
            "[F7] 같은 sfile_id 가 여러 경로에 있습니다: {}건 (예: {} → {}곳) — \
             같은 문서의 사본이면 정상입니다",
            dup_sfile_id, dups[0].0, dups[0].1.len()));
    }
    if bad > 0 {
        warnings.push(format!("[F1] 해석하지 못한 줄 {}건을 건너뛰었습니다", bad));
    }
    if no_id > 0 {
        warnings.push(format!("[F2] sfile_id 가 비어 있는 줄 {}건을 건너뛰었습니다", no_id));
    }

    // 대소문자만 다른 표기를 구제하기 위한 보조 색인. 같은 접힘 키에 둘 이상이
    // 걸리면 어느 쪽인지 알 수 없으므로 그 키는 색인에서 뺀다(조용한 오연결 방지).
    let mut fold: HashMap<String, Vec<String>> = HashMap::new();
    for k in entries.keys() {
        fold.entry(k.to_lowercase()).or_default().push(k.clone());
    }
    let folded = fold.into_iter()
        .filter(|(_, v)| v.len() == 1)
        .map(|(f, v)| (f, v[0].clone()))
        .collect();

    Ok(FileList {
        lines: lines.len(),
        comments,
        bad_lines: bad,
        no_id,
        missing_file,
        dup_sfile_id,
        entries,
        warnings,
        folded,
    })
}

//------------------------------------------------------------------
// 본문 줄들 → (줄번호, 필드맵) 목록
//=> jsonl 이냐 csv 냐를 가려서 해석한다. 판정은 첫 줄이 '{' 로 시작하는지를
//   먼저 보고, 아니면 확장자를 본다 — 확장자를 잘못 붙여 온 목록 때문에
//   통째로 실패하는 것보다 낫다.
//
//   들어오는 줄은 (물리 줄번호, 본문) 짝이다. 빈 줄·주석이 이미 빠져 있으므로
//   여기서 세면 안 되고, 달려 온 번호를 그대로 써야 사람이 찾아갈 수 있다.
//------------------------------------------------------------------
fn parse_rows(lines: &[(usize, &str)], path: &str)
    -> Result<(Vec<(usize, HashMap<String, String>)>, usize), String>
{
    let ext = Path::new(path).extension()
        .map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    let first = lines[0].1;
    let is_json = first.trim_start().starts_with('{')
        || matches!(ext.as_str(), "jsonl" | "json" | "ndjson");

    let mut rows = Vec::new();
    let mut bad = 0usize;
    if is_json {
        for (lineno, ln) in lines.iter() {
            match serde_json::from_str::<serde_json::Value>(ln) {
                Ok(v) => match v.as_object() {
                    Some(o) => {
                        let mut m = HashMap::new();
                        for (k, val) in o {
                            // 값은 문자열만 쓴다. 숫자로 온 id 도 글자로 받아 준다.
                            let s = match val {
                                serde_json::Value::String(s) => s.clone(),
                                serde_json::Value::Null => String::new(),
                                other => other.to_string(),
                            };
                            m.insert(k.clone(), s);
                        }
                        rows.push((*lineno, m));
                    }
                    None => bad += 1,
                },
                Err(_) => bad += 1,      // F1 — 그 줄만 버리고 계속 간다
            }
        }
        return Ok((rows, bad));
    }

    // csv/tsv — 첫 줄이 헤더다. 탭이 콤마보다 많으면 탭 구분으로 본다.
    let delim = if first.matches('\t').count() > first.matches(',').count() {
        '\t'
    } else {
        ','
    };
    let header: Vec<String> = split_csv(first, delim)
        .into_iter().map(|s| s.trim().to_string()).collect();
    if !header.iter().any(|h| h == "path") || !header.iter().any(|h| h == "sfile_id") {
        let shown = if header.is_empty() { "(헤더 없음)".to_string() } else { header.join(", ") };
        return Err(format!(
            "목록 {} — csv 첫 줄에 path · sfile_id 열이 필요합니다(지금: {})",
            Path::new(path).file_name().map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|| path.to_string()),
            shown));
    }
    for (lineno, ln) in lines.iter().skip(1) {
        let cells = split_csv(ln, delim);
        let mut m = HashMap::new();
        for (h, c) in header.iter().zip(cells.into_iter()) {
            m.insert(h.clone(), c);
        }
        rows.push((*lineno, m));
    }
    Ok((rows, bad))
}

/// csv 한 줄 나누기 — 큰따옴표로 감싼 칸과 그 안의 "" 이스케이프를 지킨다.
/// (경로에 콤마가 들어가면 따옴표로 감싸 오므로 최소한 그건 받아 줘야 한다.)
fn split_csv(line: &str, delim: char) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut quoted = false;
    let mut it = line.chars().peekable();
    while let Some(c) = it.next() {
        if quoted {
            if c == '"' {
                if it.peek() == Some(&'"') {
                    it.next();
                    cur.push('"');           // "" → 따옴표 한 개
                } else {
                    quoted = false;
                }
            } else {
                cur.push(c);
            }
        } else if c == '"' {
            quoted = true;
        } else if c == delim {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    out.push(cur);
    out
}

//------------------------------------------------------------------
// 문서 하나의 doc_id 정하기 — 우선순위 3단계
//=> ① 목록의 sfile_id → ② 내용 해시 → ③ 경로 해시 순으로 시도한다.
//   어느 것으로 채웠는지를 함께 돌려주어, 나중에 매핑 테이블에 넣을 때 ①만
//   골라 넣을 수 있게 한다(②·③은 MpowerV11 문서와 이어지지 않는다).
//
// -out: (doc_id, source, key, matched)
//         source  = "sfile_id" | "content" | "path"
//         matched = 목록과 이어진 방법("key"|"case") · 못 이었으면 None
//------------------------------------------------------------------
pub fn resolve_doc_id(display: &str, read_path: &Path, flist: Option<&FileList>)
    -> (Option<String>, &'static str, String, Option<&'static str>, Option<String>)
{
    // 2026-09-10: doc_id 는 MpowerV11 의 sfile_id 뿐이다. 못 얻으면 지어내지 않고
    // 비운다 — 지문(hash)이 '이 문서가 무엇인가'를 대신 가린다. 파이썬 판
    // filelist.resolve_doc_id 와 같은 계약이다(한쪽만 바꾸면 두 판이 갈린다).
    let key = normalize_key(display);
    if let Some(fl) = flist {
        if let Some((e, how)) = fl.lookup(display) {
            // 번호를 얻어도 지문은 따로 구한다 — 결합 키이자 '원본이 바뀌었나'의
            // 근거라, 번호가 있다고 없어도 되는 값이 아니다.
            let h = content_hash(read_path);
            return (Some(e.sfile_id.clone()), "sfile_id", key, Some(how), h);
        }
    }
    // 번호를 못 얻었다 — 내용 지문으로 가린다(옮기거나 이름이 바뀌어도 유지된다).
    if let Some(h) = content_hash(read_path) {
        return (None, "content", key, None, Some(h));
    }
    // 내용조차 못 읽는 파일(암호 zip·손상): 남은 단서는 경로뿐이다.
    (None, "path", key, None, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    //--------------------------------------------------------------
    // [중요] Python 판과 같은 정규화를 한다
    //=> 두 판이 다른 doc_id/key 를 내면 같은 문서가 매핑 테이블에 두 건으로
    //   들어가고, 화면이 결과를 못 잇는다. 아래 표는
    //   tests/test_docid.py::test_normalize_key_absorbs_spelling 과 같은 값이다.
    //   한쪽을 고치면 다른 쪽 시험이 빨간불이 되어야 한다.
    //--------------------------------------------------------------
    #[test]
    fn python_판과_같은_정규화를_한다() {
        let canon = "D:/collected/HWP/a.hwp";
        assert_eq!(normalize_key(r"d:\collected\HWP\a.hwp"), canon);
        assert_eq!(normalize_key("D:/collected/HWP/a.hwp"), canon);
        assert_eq!(normalize_key(r"D:\collected\x\..\HWP\a.hwp"), canon);
        // 대소문자는 보존한다(리눅스는 경로 대소문자를 구분한다).
        assert_ne!(normalize_key("D:/collected/hwp/a.hwp"), canon);
        // UNC 의 앞 슬래시 두 개는 지킨다.
        assert_eq!(normalize_key(r"\\srv\share\a.hwp"), "//srv/share/a.hwp");
        assert_eq!(normalize_key("//srv/share/a.hwp"), "//srv/share/a.hwp");
        // 절대경로의 선행 슬래시가 사라지면 다른 경로가 된다.
        assert_eq!(normalize_key("/srv/docs/./a.txt"), "/srv/docs/a.txt");
        assert_eq!(normalize_key(""), "");
    }

    //--------------------------------------------------------------
    // 지문은 SHA-256 전체(64자)다
    //=> 예전에는 이 값을 40자로 잘라 doc_id 로 썼다(그 시험이 여기 있었다).
    //   2026-09-10 부터 자르지 않고 hash 칸에 그대로 싣는다 — 파이썬 판과
    //   같은 값이어야 두 판의 결과를 나란히 견줄 수 있다.
    //--------------------------------------------------------------
    #[test]
    fn 지문은_sha256_전체다() {
        let d = std::env::temp_dir().join("mpc_hash_test.txt");
        std::fs::write(&d, b"hello").unwrap();
        let h = content_hash(&d).expect("지문을 구해야 한다");
        assert_eq!(h.len(), 64, "SHA-256 전체여야 한다: {}", h);
        // 알려진 값 — 파이썬 hashlib.sha256(b"hello").hexdigest() 와 같다.
        assert_eq!(h, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
        let _ = std::fs::remove_file(&d);
    }

    // [윈도우 전용] 이 테스트는 `D:/a.txt` 와 `d:\a.txt` 가 **같은 파일**이라는
    // 윈도우 경로 규칙(드라이브 문자 대소문자 무시 · `\` 도 구분자)에 기댄다.
    // 리눅스에서는 `D:/a.txt` 가 절대경로가 아니라 목록 파일 폴더 밑의 상대경로가
    // 되고(`/tmp/…/D:/a.txt`), `\` 는 파일명 글자라 두 줄이 서로 다른 키가 된다.
    // 그래서 F3(같은 파일에 다른 id)이 성립하지 않는다 — 구현이 틀린 게 아니라
    // 전제가 없는 것이라, 리눅스에서는 돌리지 않는다.
    #[cfg(windows)]
    #[test]
    fn f3_같은_파일에_다른_id면_멈춘다() {
        let d = std::env::temp_dir().join("csoc_fl_f3");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("l.jsonl");
        std::fs::write(&p,
            "{\"path\": \"D:/a.txt\", \"sfile_id\": \"AAA\"}\n\
             {\"path\": \"d:\\\\a.txt\", \"sfile_id\": \"BBB\"}\n").unwrap();
        let e = load(p.to_str().unwrap()).unwrap_err();
        assert!(e.contains("F3"), "F3 로 멈춰야 한다: {}", e);
    }

    //------------------------------------------------------------------
    // csv 읽기 — OS 무관 판(위 csv_도_읽는다 의 이식 가능 버전)
    //=> 위 테스트는 목록에 `D:/a.txt` 라고 적어 두어 리눅스에서는 절대경로가
    //   아니게 되는 바람에 윈도우 전용으로 묶였다. 여기서는 **실제로 만든 파일의
    //   진짜 경로**를 적어 두 OS 모두에서 절대경로가 되게 한다. 그래서 csv 해석
    //   자체(열 이름 매핑·따옴표)는 어디서 돌리든 계속 검증된다.
    //
    //   같이 확인하는 것 두 가지:
    //    1) 열 순서가 달라도 **이름으로** 찾는가(`memo,sfile_id,path` 순서로 적는다)
    //    2) 경로에 콤마가 들어가 따옴표로 감싼 칸을 제대로 푸는가
    //------------------------------------------------------------------
    #[test]
    fn csv_는_열이름으로_읽고_따옴표를_푼다() {
        let d = std::env::temp_dir().join("csoc_fl_csv_portable");
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();

        // 실제 파일을 만들어 그 경로를 목록에 적는다 — 이래야 두 OS 다 절대경로다.
        let plain = d.join("a.txt");
        std::fs::write(&plain, b"x").unwrap();
        // 이름에 콤마가 든 파일 — csv 에서는 따옴표로 감싸 와야 한다.
        let comma = d.join("b,c.txt");
        std::fs::write(&comma, b"x").unwrap();

        let p = d.join("l.csv");
        // 헤더 순서를 일부러 뒤집어 둔다(위치가 아니라 이름으로 찾는지 보려고).
        let body = format!(
            "memo,sfile_id,path\n\
             무시됨,SF-A,{}\n\
             메모2,SF-B,\"{}\"\n",
            plain.to_string_lossy(),
            comma.to_string_lossy());
        std::fs::write(&p, body).unwrap();

        let fl = load(p.to_str().unwrap()).unwrap();
        assert_eq!(fl.len(), 2, "두 줄 모두 읽혀야 한다");

        let (e, _) = fl.lookup(&plain.to_string_lossy()).unwrap();
        assert_eq!(e.sfile_id, "SF-A", "열 이름으로 sfile_id 를 찾아야 한다");

        let (e2, _) = fl.lookup(&comma.to_string_lossy()).unwrap();
        assert_eq!(e2.sfile_id, "SF-B", "따옴표로 감싼 경로(콤마 포함)를 풀어야 한다");

        // 목록에 적은 두 파일이 실제로 있으므로 F4(파일 없음) 경고가 없어야 한다.
        assert!(!fl.warnings.iter().any(|w| w.contains("F4")),
                "실재하는 파일인데 F4 경고가 났다: {:?}", fl.warnings);

        let _ = std::fs::remove_dir_all(&d);
    }

    //--------------------------------------------------------------
    // 주석 줄은 건너뛰되 '#' 로 시작하는 경로는 지킨다
    //=> 목록에 설명을 적어 둘 수 있어야 하지만, `#외부유출금지#` 같은 폴더가
    //   실제로 있으므로 '#' 뒤에 공백이 없으면 데이터로 읽어야 한다.
    //   Python 판 test_filelist_comments.py 와 같은 것을 본다.
    //--------------------------------------------------------------
    #[test]
    fn 주석줄은_건너뛰고_샾으로_시작하는_경로는_읽는다() {
        assert!(is_comment("# 설명"));
        assert!(is_comment("## 제목"));
        assert!(is_comment("   #\t들여쓴 주석"));
        assert!(is_comment("#"));
        // '#' 뒤가 글자면 경로다 — 주석이 아니다.
        assert!(!is_comment("#외부유출금지#/a.docx,SF-1"));
        assert!(!is_comment("{\"path\": \"#a/b.txt\", \"sfile_id\": \"SF-1\"}"));

        let d = std::env::temp_dir().join("csoc_fl_comment");
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join("a.txt");
        std::fs::write(&f, b"x").unwrap();

        let p = d.join("l.jsonl");
        std::fs::write(&p, format!(
            "# 이 줄은 주석\n\
             ##\n\
             \n\
             {{\"path\": {:?}, \"sfile_id\": \"SF-A\"}}\n",
            f.to_string_lossy())).unwrap();
        let fl = load(p.to_str().unwrap()).unwrap();
        assert_eq!((fl.len(), fl.lines, fl.comments), (1, 1, 2));
        // 주석은 F1(해석 실패)로 세면 안 된다.
        assert_eq!(fl.bad_lines, 0, "주석을 파싱 실패로 세면 안 된다");
        assert!(!fl.warnings.iter().any(|w| w.contains("F1")), "{:?}", fl.warnings);

        // 주석뿐이면 '빈 파일'과 다른 이유를 알려 준다.
        let p2 = d.join("only.jsonl");
        std::fs::write(&p2, "# 아무 데이터도 없다\n# 정말로\n").unwrap();
        let e = load(p2.to_str().unwrap()).unwrap_err();
        assert!(e.contains("주석"), "주석뿐임을 알려야 한다: {}", e);

        let _ = std::fs::remove_dir_all(&d);
    }

    //--------------------------------------------------------------
    // 배포에 같이 나가는 샘플 목록이 실제로 읽힌다
    //=> 샘플은 사용자가 제일 먼저 여는 파일이라, 이게 안 읽히면 첫인상이
    //   "고장난 도구"가 된다. 주석을 잔뜩 단 뒤로는 더 그렇다.
    //   Python 판 test_docid.py::test_shipped_sample_filelist_parses 와 짝이다.
    //--------------------------------------------------------------
    #[test]
    fn 배포_샘플_목록이_읽힌다() {
        let p = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("dist-onedir/windows/sample/filelist.sample.txt");
        let fl = load(p.to_str().unwrap()).expect("샘플 목록이 읽혀야 한다");
        assert_eq!(fl.len(), 7, "데이터 줄 7건이 적재돼야 한다");
        assert_eq!(fl.bad_lines, 0, "주석을 파싱 실패로 세면 안 된다");
        assert_eq!(fl.no_id, 0);
        assert!(fl.comments > 0, "주석이 있어야 한다");
        // 폴더 이름이 '#' 로 시작하는 줄도 살아 있어야 한다.
        assert!(fl.entries.keys().any(|k| k.contains("/#외부유출금지")),
                "'#' 로 시작하는 폴더 경로가 주석으로 먹히면 안 된다");
    }

    //--------------------------------------------------------------
    // 오류 메시지의 줄번호는 '편집기에서 보이는 줄'이다
    //=> 주석·빈 줄을 걷어낸 뒤의 순번을 쓰면, 주석이 많은 목록에서 F3 이 났을 때
    //   엉뚱한 줄을 가리켜 사람이 찾아가지 못한다.
    //--------------------------------------------------------------
    #[cfg(windows)]
    #[test]
    fn f3_줄번호는_물리_줄번호다() {
        let d = std::env::temp_dir().join("csoc_fl_lineno");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("l.jsonl");
        // 1~3줄이 주석/빈 줄 → 충돌 줄은 물리 5번째다.
        std::fs::write(&p,
            "# 머리말\n\
             \n\
             # 또 주석\n\
             {\"path\": \"D:/a.txt\", \"sfile_id\": \"AAA\"}\n\
             {\"path\": \"d:\\\\a.txt\", \"sfile_id\": \"BBB\"}\n").unwrap();
        let e = load(p.to_str().unwrap()).unwrap_err();
        assert!(e.contains("5번째 줄"), "물리 줄번호(5)를 가리켜야 한다: {}", e);
    }

    #[test]
    fn f1_f2_는_세고_전부_깨지면_멈춘다() {
        let d = std::env::temp_dir().join("csoc_fl_f12");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("g.jsonl");
        std::fs::write(&p,
            "이건 JSON 이 아니다\n\
             {\"path\": \"D:/a.txt\", \"sfile_id\": \"\"}\n\
             {\"path\": \"D:/b.txt\", \"sfile_id\": \"SF-B\"}\n").unwrap();
        let fl = load(p.to_str().unwrap()).unwrap();
        assert_eq!((fl.bad_lines, fl.no_id, fl.len()), (1, 1, 1));

        let p2 = d.join("b.jsonl");
        std::fs::write(&p2, "아무것도\n아니다\n").unwrap();
        assert!(load(p2.to_str().unwrap()).is_err());
    }

    // [윈도우 전용] 드라이브 문자(`D:` ↔ `d:`)와 구분자(`/` ↔ `\`)가 달라도 같은
    // 파일로 이어지는지를 보는 테스트다. 이 동등성 자체가 윈도우 규칙이라
    // 리눅스에서는 성립하지 않는다(위 f3 주석 참고).
    #[cfg(windows)]
    #[test]
    fn 대소문자만_다르면_구제한다() {
        let d = std::env::temp_dir().join("csoc_fl_case");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("l.jsonl");
        std::fs::write(&p, "{\"path\": \"D:/collected/HWP/a.hwp\", \"sfile_id\": \"SF-1\"}\n")
            .unwrap();
        let fl = load(p.to_str().unwrap()).unwrap();
        // 정규화가 흡수하는 차이(드라이브 문자·구분자)는 "key" 로 이어진다.
        let (e, how) = fl.lookup(r"d:\collected\HWP\a.hwp").unwrap();
        assert_eq!((e.sfile_id.as_str(), how), ("SF-1", "key"));
        // 폴더 이름의 대소문자는 정규화가 보존하므로 "case" 로 구제된다.
        let (e2, how2) = fl.lookup("D:/COLLECTED/hwp/a.hwp").unwrap();
        assert_eq!((e2.sfile_id.as_str(), how2), ("SF-1", "case"));
    }

    // [윈도우 전용] 확인하려는 것(csv 도 jsonl 과 같게 읽히는가) 자체는 OS 와
    // 무관하지만, 목록에 적은 `D:/a.txt` 가 리눅스에서는 절대경로가 아니라
    // lookup 키가 달라져 실패한다. 경로 규칙이 아니라 **표기** 때문에 걸린 경우다.
    #[cfg(windows)]
    #[test]
    fn csv_도_읽는다() {
        let d = std::env::temp_dir().join("csoc_fl_csv");
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("l.csv");
        std::fs::write(&p, "path,sfile_id,memo\nD:/a.txt,SF-A,무시됨\n").unwrap();
        let fl = load(p.to_str().unwrap()).unwrap();
        assert_eq!(fl.lookup("D:/a.txt").unwrap().0.sfile_id, "SF-A");
    }
}
