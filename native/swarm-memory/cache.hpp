#pragma once
#include "common.hpp"

namespace swarm {
struct Key {
    std::string object; uint64_t page;
    bool operator==(const Key& b)const{return page==b.page&&object==b.object;}
};
struct KeyHash {size_t operator()(const Key& k)const{return std::hash<std::string>{}(k.object)^(k.page*0x9e3779b97f4a7c15ULL);}};

// One cache owns all native object mounts and peer donations on this node.
// Immutable shared_ptrs pin a page only for the bounded lifetime of a reply.
// Eviction and hits are O(1), and payloads never enter a filesystem cache file.
class Cache {
    struct Entry {std::shared_ptr<const Bytes> bytes; std::list<Key>::iterator lru;};
    std::mutex mutex_; uint64_t budget_,used_=0;
    std::list<Key> lru_; std::unordered_map<Key,Entry,KeyHash> entries_;
    std::unordered_map<std::string,Bytes> inventories_;
    void mark(const Key& k,bool present) {
        auto& bits=inventories_.at(k.object);
        if(present) bits.at(k.page/8)|=1u<<(k.page%8);
        else bits.at(k.page/8)&=~(1u<<(k.page%8));
    }
public:
    explicit Cache(uint64_t budget):budget_(budget){}
    void register_object(const std::string& id,size_t pages) {
        std::lock_guard<std::mutex> l(mutex_); inventories_.emplace(id,Bytes((pages+7)/8));
    }
    std::shared_ptr<const Bytes> get(const Key& k) {
        std::lock_guard<std::mutex> l(mutex_); auto i=entries_.find(k); if(i==entries_.end())return {};
        lru_.splice(lru_.begin(),lru_,i->second.lru); return i->second.bytes;
    }
    bool put(const Key& k,std::shared_ptr<const Bytes> data) {
        std::lock_guard<std::mutex> l(mutex_);
        auto inv=inventories_.find(k.object);
        if(inv==inventories_.end()||k.page/8>=inv->second.size()||data->size()>budget_)return false;
        if(entries_.count(k))return true;
        while(used_+data->size()>budget_&&!lru_.empty()) {
            auto victim=lru_.back();auto i=entries_.find(victim);used_-=i->second.bytes->size();
            mark(victim,false);entries_.erase(i);lru_.pop_back();
        }
        lru_.push_front(k);used_+=data->size();entries_.emplace(k,Entry{std::move(data),lru_.begin()});mark(k,true);return true;
    }
    Bytes inventory(const std::string& id) {std::lock_guard<std::mutex> l(mutex_);return inventories_.at(id);}
    uint64_t used(){std::lock_guard<std::mutex> l(mutex_);return used_;}
    uint64_t budget()const{return budget_;}
};
}
