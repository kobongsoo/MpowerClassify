// moaipii.hpp — moaipii.dll / libmoaipii.so 의 C++ 감싸개 (헤더 하나, C++17, 외부 의존 없음)
//
// 정본: MoAI-pii/bindings/cpp/moaipii.hpp (배포 묶음에서는 include/moaipii.hpp)
//
// 두 가지 방식
//   링크형(기본)   : 빌드할 때 라이브러리를 링크한다.
//                    Windows  cl /std:c++17 /EHsc /utf-8 app.cpp /I include lib\moaipii.dll.lib
//                    Linux    g++ -std=c++17 app.cpp -I include -L lib -lmoaipii -Wl,-rpath,'$ORIGIN'
//   동적 로드형    : #define MOAIPII_DYNAMIC 을 이 헤더보다 먼저 두면 링크하지 않고
//                    실행 중에 LoadLibraryW / dlopen 으로 찾는다(Linux 는 -ldl 이 필요할 수 있다).
//                    찾는 순서: load(path) 인자 → 환경변수 MOAIPII_LIB(파일·폴더) → 실행파일 폴더 → 이름만(시스템)
//
// 사용
//   auto pii = moaipii::Lib::load();
//   auto r   = pii.scan(text, {"RRN", "PHONE"});             // 좌표는 기본 byte — text.substr(start, end-start)
//   for (auto& [label, n] : r.counts) std::cout << label << " " << n << "\n";
//
// 규칙은 C 헤더 moaipii.h 와 같다. 라이브러리가 준 문자열은 읽은 즉시 moaipii_free 로 돌려준다
// (예외가 나도 unique_ptr 이 돌려준다). Lib 은 복사해도 되고 여러 스레드에서 같이 써도 된다.
#ifndef MOAIPII_HPP
#define MOAIPII_HPP

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

#include "moaipii.h"

#ifdef MOAIPII_DYNAMIC
#  ifdef _WIN32
#    ifndef WIN32_LEAN_AND_MEAN
#      define WIN32_LEAN_AND_MEAN
#    endif
#    ifndef NOMINMAX
#      define NOMINMAX
#    endif
#    include <windows.h>
#  else
#    include <dlfcn.h>
#    include <unistd.h>
#    include <climits>
#  endif
#  include <sys/stat.h>
#endif

namespace moaipii {

// 실패. kind = "arg"|"utf8"|"panic"|"load"|"abi". code 는 라이브러리 반환 코드(불러오기 실패면 -1).
struct error : std::runtime_error {
    std::string kind;
    int code;
    error(int c, std::string k, const std::string& msg) : std::runtime_error(msg), kind(std::move(k)), code(c) {}
};

// 검사 옵션. 기본값: 위치 있음 · 값 없음 · byte 좌표 · 깨진 UTF-8 은 오류 · 모르는 라벨은 오류.
struct Options {
    bool items = true;
    bool values = false;
    std::string offsets = "byte";          // "byte"(C++ 기본) | "char" | "utf16"
    std::string invalid_utf8 = "error";    // "error" | "replace"
    std::string unknown_labels = "error";  // "error" | "ignore"
    std::optional<std::string> today;      // 없으면 시스템 날짜, 있으면 "YYYY-MM-DD"
};

// 검출 한 건. 좌표는 [start, end). value 는 Options::values 일 때만.
struct Item {
    std::string label;
    std::size_t start = 0;
    std::size_t end = 0;
    std::optional<std::string> value;
};

// 검사 결과.
struct Result {
    std::uint64_t chars = 0;                        // 본문 코드포인트 수
    std::map<std::string, std::uint64_t> counts;    // 라벨별 건수(0 건은 없다)
    std::vector<Item> items;                        // 겹침 해소가 끝난 위치 목록(문서 순서)
    std::vector<std::string> unknown;               // 무시한 모르는 라벨
    std::string json;                               // 라이브러리가 준 원문 JSON
};

namespace detail {

// ---- 아주 작은 JSON 값·해석기 (결과 JSON 을 읽는 데만 쓴다) ----
struct Value;
using Array = std::vector<Value>;
using Object = std::vector<std::pair<std::string, Value>>;
struct Value {
    std::variant<std::nullptr_t, bool, double, std::string, std::shared_ptr<Array>, std::shared_ptr<Object>> v = nullptr;
    // 객체에서 키 하나 찾기(없으면 nullptr)
    const Value* get(std::string_view k) const {
        if (auto o = std::get_if<std::shared_ptr<Object>>(&v)) {
            for (auto& [kk, vv] : **o) if (kk == k) return &vv;
        }
        return nullptr;
    }
    const std::string* str() const { return std::get_if<std::string>(&v); }
    double num() const { auto d = std::get_if<double>(&v); return d ? *d : 0.0; }
    const Array* arr() const { auto a = std::get_if<std::shared_ptr<Array>>(&v); return a ? a->get() : nullptr; }
    const Object* obj() const { auto o = std::get_if<std::shared_ptr<Object>>(&v); return o ? o->get() : nullptr; }
};

class Parser {
public:
    explicit Parser(std::string_view s) : s_(s) {}
    // 문서 전체를 읽는다. 형식이 틀리면 error("panic")
    Value parse() {
        ws();
        Value v = value();
        ws();
        if (i_ != s_.size()) fail("뒤에 남은 글자");
        return v;
    }

private:
    std::string_view s_;
    std::size_t i_ = 0;

    [[noreturn]] void fail(const char* why) const {
        throw error(MP_ERR_PANIC, "panic", std::string("결과 JSON 오류: ") + why + " (" + std::to_string(i_) + ")");
    }
    void ws() { while (i_ < s_.size() && (s_[i_] == ' ' || s_[i_] == '\n' || s_[i_] == '\r' || s_[i_] == '\t')) ++i_; }
    bool lit(std::string_view w) {
        if (s_.substr(i_, w.size()) == w) { i_ += w.size(); return true; }
        return false;
    }
    Value value() {
        if (i_ >= s_.size()) fail("값이 없음");
        char c = s_[i_];
        Value out;
        if (c == '{') {
            ++i_;
            auto o = std::make_shared<Object>();
            ws();
            if (i_ < s_.size() && s_[i_] == '}') { ++i_; out.v = o; return out; }
            for (;;) {
                ws();
                if (i_ >= s_.size() || s_[i_] != '"') fail("객체 키가 아님");
                std::string k = string();
                ws();
                if (i_ >= s_.size() || s_[i_] != ':') fail("':' 없음");
                ++i_;
                ws();
                o->emplace_back(std::move(k), value());
                ws();
                if (i_ < s_.size() && s_[i_] == ',') { ++i_; continue; }
                if (i_ < s_.size() && s_[i_] == '}') { ++i_; break; }
                fail("객체가 닫히지 않음");
            }
            out.v = o;
        } else if (c == '[') {
            ++i_;
            auto a = std::make_shared<Array>();
            ws();
            if (i_ < s_.size() && s_[i_] == ']') { ++i_; out.v = a; return out; }
            for (;;) {
                ws();
                a->push_back(value());
                ws();
                if (i_ < s_.size() && s_[i_] == ',') { ++i_; continue; }
                if (i_ < s_.size() && s_[i_] == ']') { ++i_; break; }
                fail("배열이 닫히지 않음");
            }
            out.v = a;
        } else if (c == '"') {
            out.v = string();
        } else if (lit("true")) {
            out.v = true;
        } else if (lit("false")) {
            out.v = false;
        } else if (lit("null")) {
            out.v = nullptr;
        } else {
            std::size_t st = i_;
            while (i_ < s_.size() && (std::strchr("+-0123456789.eE", s_[i_]) != nullptr)) ++i_;
            if (st == i_) fail("모르는 값");
            out.v = std::strtod(std::string(s_.substr(st, i_ - st)).c_str(), nullptr);
        }
        return out;
    }
    // 코드포인트 하나를 UTF-8 로 붙인다
    static void put_utf8(std::string& o, std::uint32_t cp) {
        if (cp < 0x80) o += char(cp);
        else if (cp < 0x800) { o += char(0xC0 | (cp >> 6)); o += char(0x80 | (cp & 0x3F)); }
        else if (cp < 0x10000) { o += char(0xE0 | (cp >> 12)); o += char(0x80 | ((cp >> 6) & 0x3F)); o += char(0x80 | (cp & 0x3F)); }
        else { o += char(0xF0 | (cp >> 18)); o += char(0x80 | ((cp >> 12) & 0x3F)); o += char(0x80 | ((cp >> 6) & 0x3F)); o += char(0x80 | (cp & 0x3F)); }
    }
    std::uint32_t hex4() {
        if (i_ + 4 > s_.size()) fail("\\u 뒤가 짧음");
        std::uint32_t v = 0;
        for (int k = 0; k < 4; ++k) {
            char h = s_[i_++];
            v <<= 4;
            if (h >= '0' && h <= '9') v |= std::uint32_t(h - '0');
            else if (h >= 'a' && h <= 'f') v |= std::uint32_t(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') v |= std::uint32_t(h - 'A' + 10);
            else fail("\\u 16진수가 아님");
        }
        return v;
    }
    std::string string() {
        ++i_;  // 여는 따옴표
        std::string o;
        while (i_ < s_.size()) {
            char c = s_[i_++];
            if (c == '"') return o;
            if (c != '\\') { o += c; continue; }
            if (i_ >= s_.size()) break;
            char e = s_[i_++];
            switch (e) {
                case '"': o += '"'; break;
                case '\\': o += '\\'; break;
                case '/': o += '/'; break;
                case 'b': o += '\b'; break;
                case 'f': o += '\f'; break;
                case 'n': o += '\n'; break;
                case 'r': o += '\r'; break;
                case 't': o += '\t'; break;
                case 'u': {
                    std::uint32_t cp = hex4();
                    // 서로게이트 짝이면 합친다
                    if (cp >= 0xD800 && cp <= 0xDBFF && s_.substr(i_, 2) == "\\u") {
                        std::size_t save = i_;
                        i_ += 2;
                        std::uint32_t lo = hex4();
                        if (lo >= 0xDC00 && lo <= 0xDFFF) cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        else i_ = save;
                    }
                    put_utf8(o, cp);
                    break;
                }
                default: fail("모르는 이스케이프");
            }
        }
        fail("문자열이 닫히지 않음");
    }
};

// 문자열 → JSON 문자열 표기
inline std::string quote(std::string_view s) {
    std::string o = "\"";
    for (unsigned char c : s) {
        switch (c) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); o += b; }
                else o += char(c);
        }
    }
    return o + "\"";
}

// 라이브러리 함수 포인터 한 벌
struct Fns {
    std::uint32_t (*abi)() = nullptr;
    const char* (*version)() = nullptr;
    const char* (*basis)() = nullptr;
    int (*labels)(char**) = nullptr;
    int (*scan)(const std::uint8_t*, std::size_t, const char*, char**) = nullptr;
    void (*free_)(char*) = nullptr;
};

}  // namespace detail

class Lib {
public:
    // 라이브러리를 연다. 링크형이면 path 는 쓰지 않는다(링크한 것을 쓴다).
    // 실패: error("load") 못 찾음·함수 없음, error("abi") 함수 모양 판이 다름
    static Lib load(const std::string& path = std::string()) {
        detail::Fns f;
        std::string where;
#ifdef MOAIPII_DYNAMIC
        open_dynamic(path, f, where);
#else
        (void)path;
        f.abi = &moaipii_abi_version;
        f.version = &moaipii_version;
        f.basis = &moaipii_kopii_basis;
        f.labels = &moaipii_labels;
        f.scan = &moaipii_scan;
        f.free_ = &moaipii_free;
        where = "(링크됨)";
#endif
        std::uint32_t abi = f.abi();
        if (abi != MOAIPII_ABI_VERSION) {
            throw error(-1, "abi", "moaipii ABI 판이 다릅니다: 라이브러리 " + std::to_string(abi) +
                                   ", 감싸개 " + std::to_string(MOAIPII_ABI_VERSION) + " (" + where + ")");
        }
        return Lib(f, where);
    }

    const std::string& lib_path() const { return path_; }
    std::string version() const { return f_.version(); }
    std::string kopii_basis() const { return f_.basis(); }

    // 지원 라벨 목록(별칭 제외)
    std::vector<std::string> labels() const {
        auto [code, json] = call([&](char** o) { return f_.labels(o); });
        detail::Value v = checked(code, json);
        std::vector<std::string> out;
        if (auto a = v.get("labels") ? v.get("labels")->arr() : nullptr)
            for (auto& x : *a) if (auto s = x.str()) out.push_back(*s);
        return out;
    }

    // 옵션 JSON 을 직접 넘기고 (반환 코드, 원문 JSON) 을 받는다 — 실패도 예외 없이 코드로.
    std::pair<int, std::string> scan_json(std::string_view text, const std::string& opts_json) const {
        return call([&](char** o) {
            return f_.scan(reinterpret_cast<const std::uint8_t*>(text.data()), text.size(), opts_json.c_str(), o);
        });
    }

    // 본문 검사. 실패: error("arg"|"utf8"|"panic")
    Result scan(std::string_view text, const std::vector<std::string>& labels, const Options& o = Options()) const {
        std::string j = "{\"labels\":[";
        for (std::size_t k = 0; k < labels.size(); ++k) { if (k) j += ','; j += detail::quote(labels[k]); }
        j += "],\"items\":";
        j += o.items ? "true" : "false";
        j += ",\"values\":";
        j += o.values ? "true" : "false";
        j += ",\"offsets\":" + detail::quote(o.offsets);
        j += ",\"invalid_utf8\":" + detail::quote(o.invalid_utf8);
        j += ",\"unknown_labels\":" + detail::quote(o.unknown_labels);
        j += ",\"today\":" + (o.today ? detail::quote(*o.today) : std::string("null"));
        j += "}";
        auto [code, json] = scan_json(text, j);
        detail::Value v = checked(code, json);
        Result r;
        r.json = json;
        if (auto c = v.get("chars")) r.chars = static_cast<std::uint64_t>(c->num());
        if (auto c = v.get("counts")) if (auto ob = c->obj())
            for (auto& [k, n] : *ob) r.counts[k] = static_cast<std::uint64_t>(n.num());
        if (auto a = v.get("items")) if (auto arr = a->arr())
            for (auto& x : *arr) {
                Item it;
                if (auto l = x.get("label")) if (auto s = l->str()) it.label = *s;
                if (auto s = x.get("start")) it.start = static_cast<std::size_t>(s->num());
                if (auto e = x.get("end")) it.end = static_cast<std::size_t>(e->num());
                if (auto val = x.get("value")) if (auto s = val->str()) it.value = *s;
                r.items.push_back(std::move(it));
            }
        if (auto a = v.get("unknown")) if (auto arr = a->arr())
            for (auto& x : *arr) if (auto s = x.str()) r.unknown.push_back(*s);
        return r;
    }

private:
    detail::Fns f_;
    std::string path_;
    Lib(detail::Fns f, std::string p) : f_(f), path_(std::move(p)) {}

    // out 문자열을 받는 호출 → (코드, 사본). 예외가 나도 라이브러리 메모리는 돌려준다.
    template <class F>
    std::pair<int, std::string> call(F&& fn) const {
        char* raw = nullptr;
        int code = fn(&raw);
        auto del = [this](char* p) { if (p) f_.free_(p); };
        std::unique_ptr<char, decltype(del)> hold(raw, del);
        return {code, raw ? std::string(raw) : std::string()};
    }

    // 결과 JSON → 값, 실패 코드면 예외
    static detail::Value checked(int code, const std::string& json) {
        detail::Value v = json.empty() ? detail::Value() : detail::Parser(json).parse();
        if (code != MP_OK) {
            const detail::Value* e = v.get("error");
            std::string kind = "panic", msg = "알 수 없는 오류";
            if (e) {
                if (auto k = e->get("kind")) if (auto s = k->str()) kind = *s;
                if (auto m = e->get("message")) if (auto s = m->str()) msg = *s;
            }
            throw error(code, kind, msg);
        }
        return v;
    }

#ifdef MOAIPII_DYNAMIC
    static bool is_dir(const std::string& p) {
        struct stat st;
        return stat(p.c_str(), &st) == 0 && (st.st_mode & S_IFDIR);
    }
    static bool is_file(const std::string& p) {
        struct stat st;
        return stat(p.c_str(), &st) == 0 && (st.st_mode & S_IFREG);
    }
#  ifdef _WIN32
    static constexpr const char* kFile = "moaipii.dll";
    static constexpr char kSep = '\\';
    // UTF-8 경로 → 넓은 문자 경로 (한글 경로가 깨지지 않게)
    static std::wstring wide(const std::string& s) {
        int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
        std::wstring w(n > 0 ? n - 1 : 0, L'\0');
        if (n > 1) MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
        return w;
    }
    static void* open_one(const std::string& p) { return (void*)LoadLibraryW(wide(p).c_str()); }
    static void* sym(void* h, const char* n) { return (void*)GetProcAddress((HMODULE)h, n); }
    static std::string exe_dir() {
        wchar_t b[4096];
        DWORD n = GetModuleFileNameW(nullptr, b, 4096);
        std::wstring w(b, n);
        auto k = w.find_last_of(L"\\/");
        w = k == std::wstring::npos ? L"." : w.substr(0, k);
        int m = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr, nullptr);
        std::string s(m > 0 ? m - 1 : 0, '\0');
        if (m > 1) WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, &s[0], m, nullptr, nullptr);
        return s;
    }
#  else
    static constexpr const char* kFile = "libmoaipii.so";
    static constexpr char kSep = '/';
    static void* open_one(const std::string& p) { return dlopen(p.c_str(), RTLD_NOW | RTLD_LOCAL); }
    static void* sym(void* h, const char* n) { return dlsym(h, n); }
    static std::string exe_dir() {
        char b[PATH_MAX];
        ssize_t n = readlink("/proc/self/exe", b, sizeof b - 1);
        std::string s = n > 0 ? std::string(b, static_cast<std::size_t>(n)) : std::string(".");
        auto k = s.find_last_of('/');
        return k == std::string::npos ? "." : s.substr(0, k);
    }
#  endif
    // 후보를 차례로 열고 함수 포인터를 채운다. 라이브러리는 프로세스가 끝날 때까지 열어 둔다.
    static void open_dynamic(const std::string& path, detail::Fns& f, std::string& where) {
        std::vector<std::string> cands;
        auto add = [&](const std::string& p) { cands.push_back(is_dir(p) ? p + kSep + kFile : p); };
        if (!path.empty()) add(path);
        else {
            if (const char* e = std::getenv("MOAIPII_LIB")) if (*e) add(e);
            add(exe_dir() + kSep + kFile);
            cands.push_back(kFile);  // 이름만 — 시스템 경로에서 찾는다
        }
        std::string tried;
        for (auto& c : cands) {
            bool has_path = c.find('/') != std::string::npos || c.find('\\') != std::string::npos;
            if (has_path && !is_file(c)) { tried += c + " , "; continue; }
            void* h = open_one(c);
            if (!h) { tried += c + "(열지 못함) , "; if (!path.empty()) break; continue; }
            f.abi = reinterpret_cast<std::uint32_t (*)()>(sym(h, "moaipii_abi_version"));
            f.version = reinterpret_cast<const char* (*)()>(sym(h, "moaipii_version"));
            f.basis = reinterpret_cast<const char* (*)()>(sym(h, "moaipii_kopii_basis"));
            f.labels = reinterpret_cast<int (*)(char**)>(sym(h, "moaipii_labels"));
            f.scan = reinterpret_cast<int (*)(const std::uint8_t*, std::size_t, const char*, char**)>(sym(h, "moaipii_scan"));
            f.free_ = reinterpret_cast<void (*)(char*)>(sym(h, "moaipii_free"));
            if (!f.abi) throw error(-1, "load", "moaipii_abi_version 함수가 없습니다: " + c);
            // ABI 가 맞으면 나머지 함수도 다 있어야 한다(ABI 검사는 load 가 한다)
            if (f.abi() == MOAIPII_ABI_VERSION && (!f.version || !f.basis || !f.labels || !f.scan || !f.free_))
                throw error(-1, "load", "moaipii 함수가 빠져 있습니다: " + c);
            where = c;
            return;
        }
        throw error(-1, "load", "moaipii 라이브러리를 찾지 못했습니다. 찾아본 곳: " + tried + "(환경변수 MOAIPII_LIB 로 알려 주세요)");
    }
#endif
};

}  // namespace moaipii

#endif  // MOAIPII_HPP
