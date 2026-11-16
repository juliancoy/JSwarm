#include "cache.hpp"
#include <iostream>
using namespace swarm;
void require(bool value){if(!value)throw std::runtime_error("Cache assertion failed");}
int main(){
    Cache cache(8192);cache.register_object("a",1024);cache.register_object("b",1024);
    auto data=std::make_shared<const Bytes>(4096,42);
    require(cache.put({"a",0},data));auto pinned=cache.get({"a",0});
    require(cache.put({"b",0},data));require(bool(cache.get({"a",0})));
    require(cache.put({"a",1},data));require(!cache.get({"b",0}));
    require((cache.inventory("a")[0]&3)==3);require(cache.inventory("b")[0]==0);
    require(cache.put({"b",1},data));require(!cache.get({"a",0}));require(pinned->at(0)==42);
    std::vector<std::thread> threads;
    for(int t=0;t<8;t++)threads.emplace_back([&,t]{for(int i=0;i<1000;i++){Key k{t%2?"a":"b",uint64_t((i+t)%1024)};cache.put(k,data);auto hit=cache.get(k);if(hit)require(hit->size()==4096);require(cache.used()<=8192);}});
    for(auto& t:threads)t.join();
    require(cache.used()<=8192);std::cout<<"bounded multi-object cache, LRU, pinned readers and concurrent eviction passed\n";
}
