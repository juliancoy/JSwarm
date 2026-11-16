#pragma once
#include "cache.hpp"
#include "transport.hpp"
#include <set>

namespace swarm {
struct Object {
    std::string id,sha,mountpoint;uint64_t bytes;size_t page_bytes;
    std::vector<Digest> hashes;Json sources;
    std::vector<std::string> peers;std::set<std::string> allowed;
    bool allow_other=false,allow_root=false;
    size_t length(uint64_t page)const {
        if(page>=hashes.size())throw std::runtime_error("Page outside object");
        return std::min<uint64_t>(page_bytes,bytes-page*page_bytes);
    }
    bool verify(uint64_t page,const Bytes& data)const{return page<hashes.size()&&data.size()==length(page)&&hash(data)==hashes[page];}
};
class Engine {
    struct Summary {Bytes bits;Clock::time_point expires;};
    Json config_;
    std::array<std::mutex,256> stripes_;
    std::mutex peer_mutex_,error_mutex_;
    std::unordered_map<std::string,Summary> summaries_;
    std::unordered_map<std::string,Clock::time_point> cooldown_;
    std::string last_error_;
    Pool replicas_{2,16};
    std::shared_ptr<const Bytes> peer_page(const Object& object,uint64_t page,bool refresh=false);
    void publish(const Object& object,uint64_t page,std::shared_ptr<const Bytes> data);
public:
    Cache cache;Transport transport;Counters counters;
    std::unordered_map<std::string,std::shared_ptr<Object>> objects;
    Pool reads{8,128};
    explicit Engine(Json config);
    ~Engine(){reads.stop();replicas_.stop();}
    std::shared_ptr<const Bytes> page(const Object& object,uint64_t number);
    Bytes read(const Object& object,uint64_t offset,size_t length);
    std::string metrics(const Object& object);
    void serve(std::atomic<bool>& stop);
    void error(const std::string& what){std::lock_guard<std::mutex> l(error_mutex_);last_error_=what;counters.add("read_errors");}
};
}
