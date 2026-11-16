#pragma once
#include <nlohmann/json.hpp>
#include <openssl/sha.h>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <fstream>
#include <functional>
#include <iomanip>
#include <list>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace swarm {
using Json = nlohmann::json;
using Bytes = std::vector<unsigned char>;
using Clock = std::chrono::steady_clock;
using Digest = std::array<unsigned char, SHA256_DIGEST_LENGTH>;
inline Digest hash(const unsigned char* p, size_t n) { Digest d{}; SHA256(p,n,d.data()); return d; }
inline Digest hash(const Bytes& b) { return hash(b.data(),b.size()); }
inline std::string hex(const Digest& d) {
    std::ostringstream s; s << std::hex << std::setfill('0');
    for(auto c:d) s << std::setw(2) << unsigned(c);
    return s.str();
}
inline Digest unhex(const std::string& s) {
    if(s.size()!=64 || s.find_first_not_of("0123456789abcdef")!=std::string::npos) throw std::runtime_error("Invalid digest");
    Digest d{}; for(size_t i=0;i<32;i++) d[i]=std::stoul(s.substr(i*2,2),nullptr,16); return d;
}
inline Json read_json(const std::string& p) { std::ifstream f(p); if(!f) throw std::runtime_error("Cannot open configuration"); Json j; f>>j; return j; }
inline uint64_t seconds_now() { return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count(); }
struct IntegrityError : std::runtime_error { using runtime_error::runtime_error; };

class Counters {
    std::mutex mutex_; std::unordered_map<std::string,uint64_t> values_;
public:
    void add(const std::string& k,uint64_t n=1) { std::lock_guard<std::mutex> l(mutex_); values_[k]+=n; }
    Json snapshot() { std::lock_guard<std::mutex> l(mutex_); return values_; }
};

// A bounded queue also bounds outstanding response buffers. Work submitted by
// libfuse is completed asynchronously, so its request loop never performs I/O.
class Pool {
    std::mutex mutex_; std::condition_variable ready_;
    std::deque<std::function<void()>> work_; std::vector<std::thread> threads_;
    size_t limit_; bool stopping_=false;
public:
    Pool(size_t count,size_t limit):limit_(limit) {
        for(size_t i=0;i<count;i++) threads_.emplace_back([this]{
            while(true) { std::function<void()> f;
                { std::unique_lock<std::mutex> l(mutex_); ready_.wait(l,[this]{return stopping_||!work_.empty();});
                  if(work_.empty()&&stopping_) return;
                  f=std::move(work_.front()); work_.pop_front(); }
                try { f(); } catch(...) { /* Every operation owns its error reply. */ }
            }
        });
    }
    bool submit(std::function<void()> f) {
        std::lock_guard<std::mutex> l(mutex_); if(stopping_||work_.size()>=limit_) return false;
        work_.push_back(std::move(f)); ready_.notify_one(); return true;
    }
    void stop() {
        {std::lock_guard<std::mutex> l(mutex_); stopping_=true;} ready_.notify_all();
        for(auto& t:threads_) if(t.joinable()) t.join();
    }
    ~Pool(){stop();}
};
}
