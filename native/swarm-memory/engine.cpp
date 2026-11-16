#include "engine.hpp"
#include <algorithm>
#include <iostream>
#include <netinet/in.h>
#include <sys/socket.h>
#include <poll.h>
#include <unistd.h>

namespace swarm {
Engine::Engine(Json config):config_(std::move(config)),cache(config_.at("cache_bytes")),transport(config_) {
    if(cache.budget()<4096||cache.budget()>1024ULL*1024*1024)throw std::runtime_error("Invalid node cache budget");
    if(config_.at("objects").empty()||config_.at("objects").size()>256)throw std::runtime_error("Invalid object count");
    for(const auto& row:config_.at("objects")) {
        const auto& manifest=row.at("manifest");auto object=std::make_shared<Object>();
        Json core;for(auto key:{"schema","bytes","page_bytes","sha256","pages"})core[key]=manifest.at(key);
        auto canonical=core.dump();auto ident=hex(hash(reinterpret_cast<const unsigned char*>(canonical.data()),canonical.size()));
        if(manifest.at("schema")!="jswarm-memory-object.v1"||manifest.at("id")!=ident)throw IntegrityError("Object manifest identity differs");
        object->id=ident;object->sha=manifest.at("sha256");unhex(object->sha);
        object->bytes=manifest.at("bytes");object->page_bytes=manifest.at("page_bytes");
        if(!object->bytes||object->bytes>(1ULL<<48)||object->page_bytes<4096||object->page_bytes>262144||(object->page_bytes&(object->page_bytes-1)))throw IntegrityError("Invalid object dimensions");
        if(manifest.at("pages").size()!=(object->bytes+object->page_bytes-1)/object->page_bytes||manifest.at("pages").size()>8*1024*1024)throw IntegrityError("Invalid page directory");
        for(const auto& sha:manifest.at("pages"))object->hashes.push_back(unhex(sha));
        object->mountpoint=row.at("mountpoint");object->sources=row.at("sources");
        object->peers=row.at("peers").get<std::vector<std::string>>();
        object->allow_other=row.value("allow_other",false);object->allow_root=row.value("allow_root",false);
        object->allowed=row.at("allowed").get<std::set<std::string>>();
        if(!object->allowed.count(transport.name()))throw IntegrityError("Node lacks object authorization");
        for(const auto& peer:object->peers)if(!object->allowed.count(peer)||!transport.enrollment().at("peers").contains(peer))throw IntegrityError("Unenrolled cache peer");
        for(const auto& source:object->sources) {
            if(source.at("sha256")!=object->sha||source.at("bytes")!=object->bytes||
               source.at("machine")!=transport.enrollment().at("peers").at(source.at("source").get<std::string>()).at("machine"))throw IntegrityError("Source identity differs");
            unhex(source.at("clients").at(transport.name()));
        }
        cache.register_object(object->id,object->hashes.size());
        if(!objects.emplace(object->id,object).second)throw IntegrityError("Duplicate object mount");
    }
    // Keep compact binary hashes, not the parsed per-page JSON strings.
    config_.erase("objects");
}

std::shared_ptr<const Bytes> Engine::peer_page(const Object& object,uint64_t page,bool refresh) {
    for(const auto& peer:object.peers) {
        if(peer==transport.name())continue;
        auto key=peer+object.id;bool present=false,query=false;
        {
            std::lock_guard<std::mutex> l(peer_mutex_);auto now=Clock::now();
            if(cooldown_[peer]>now)continue;
            auto i=summaries_.find(key);query=refresh||i==summaries_.end()||i->second.expires<now;
            if(!query)present=i->second.bits[page/8]&(1u<<(page%8));
        }
        try {
            int port=config_.value("peer_ports",Json::object()).value(peer,config_.at("peer_port").get<int>());
            if(query) {
                auto r=transport.request(peer,port,"/v1/cache/"+object.id+"/inventory");counters.add("inventory_requests");
                if(r.status!=200||r.body.size()!=(object.hashes.size()+7)/8)throw std::runtime_error("Peer inventory unavailable");
                present=r.body[page/8]&(1u<<(page%8));
                std::lock_guard<std::mutex> l(peer_mutex_);summaries_[key]={std::move(r.body),Clock::now()+std::chrono::seconds(2)};
            }
            if(!present){counters.add("peer_probes_avoided");continue;}
            auto r=transport.request(peer,port,"/v1/cache/"+object.id+"/"+std::to_string(page));counters.add("peer_requests");
            if(r.status==404){counters.add("peer_misses");continue;}
            if(r.status!=200||!object.verify(page,r.body)){counters.add("corrupt_peer_pages");throw std::runtime_error("Peer page rejected");}
            counters.add("peer_hits");return std::make_shared<const Bytes>(std::move(r.body));
        } catch(const std::exception&) {counters.add("peer_errors");std::lock_guard<std::mutex> l(peer_mutex_);cooldown_[peer]=Clock::now()+std::chrono::seconds(5);}
    }
    return {};
}

void Engine::publish(const Object& object,uint64_t page,std::shared_ptr<const Bytes> data) {
    size_t count=std::min<size_t>(config_.value("replicas",2),object.peers.size());if(!count)return;
    auto ranked=object.peers;std::string key=object.id+":"+std::to_string(page);
    std::sort(ranked.begin(),ranked.end(),[&](const std::string& a,const std::string& b){return std::hash<std::string>{}(key+a)>std::hash<std::string>{}(key+b);});
    if(!replicas_.submit([this,ranked=std::move(ranked),key,object_id=object.id,page,data,count]{
        size_t stored=0;
        for(const auto& peer:ranked) {
            {std::lock_guard<std::mutex> l(peer_mutex_);if(cooldown_[peer]>Clock::now())continue;}
            try {
                int port=config_.value("peer_ports",Json::object()).value(peer,config_.at("peer_port").get<int>());
                auto r=transport.request(peer,port,"/v1/cache/"+object_id+"/"+std::to_string(page),{},*data,true);
                if(r.status==201){counters.add("replicas_stored");if(++stored>=count)break;}else counters.add("replica_admission_denied");
            } catch(const std::exception&) {std::lock_guard<std::mutex> l(peer_mutex_);cooldown_[peer]=Clock::now()+std::chrono::seconds(5);counters.add("replica_errors");}
        }
    }))counters.add("replication_backpressure");
}

std::shared_ptr<const Bytes> Engine::page(const Object& object,uint64_t number) {
    object.length(number);Key key{object.id,number};auto hit=cache.get(key);
    if(hit){counters.add("local_hits");return hit;}
    // Adjacent faults share one coalesced source transfer, up to 256 KiB.
    size_t group=262144/object.page_bytes;uint64_t first=number/group*group;
    std::unique_lock<std::mutex> lock(stripes_[KeyHash{}({object.id,first})%stripes_.size()]);
    hit=cache.get(key);if(hit){counters.add("coalesced_hits");return hit;}
    hit=peer_page(object,number);
    if(hit){cache.put(key,hit);return hit;}
    uint64_t offset=first*object.page_bytes;size_t length=std::min<uint64_t>(262144,object.bytes-offset);
    for(const auto& source:object.sources) {
        try {
            std::string grant=source.at("clients").at(transport.name());
            auto r=transport.request(source.at("source"),transport.enrollment().at("port"),"/objects/"+grant,
                {{"Range","bytes="+std::to_string(offset)+"-"+std::to_string(offset+length-1)},{"If-Match","\""+object.sha+"\""}});
            std::string range="bytes "+std::to_string(offset)+"-"+std::to_string(offset+length-1)+"/"+std::to_string(object.bytes);
            if(r.status!=206||r.headers["etag"]!="\""+object.sha+"\""||r.headers["content-range"]!=range||r.body.size()!=length)throw IntegrityError("Source range identity differs");
            std::vector<std::pair<uint64_t,std::shared_ptr<const Bytes>>> verified;
            for(size_t at=0;at<length;at+=object.page_bytes) {
                uint64_t page=first+at/object.page_bytes;size_t n=object.length(page);
                auto part=std::make_shared<const Bytes>(r.body.begin()+at,r.body.begin()+at+n);
                if(!object.verify(page,*part))throw IntegrityError("Source page hash differs");
                verified.emplace_back(page,std::move(part));
            }
            counters.add("source_requests");counters.add("source_reads",verified.size());counters.add("source_bytes",length);
            for(auto& part:verified){cache.put({object.id,part.first},part.second);publish(object,part.first,part.second);if(part.first==number)hit=part.second;}
            return hit;
        } catch(const IntegrityError&){counters.add("integrity_failures");throw;}
          catch(const std::exception& e){counters.add("source_transport_errors");std::lock_guard<std::mutex> l(error_mutex_);last_error_=e.what();}
    }
    // A stale inventory is only a hint. Refresh it after source failure before
    // concluding that an uncached page is unavailable.
    hit=peer_page(object,number,true);if(hit){cache.put(key,hit);return hit;}
    throw std::runtime_error("Page unavailable from admitted peers and sources");
}
Bytes Engine::read(const Object& object,uint64_t offset,size_t length) {
    if(length>1024*1024)throw std::runtime_error("Read exceeds request bound");
    if(offset>=object.bytes)return {};
    length=std::min<uint64_t>(length,object.bytes-offset);Bytes result(length);size_t done=0;
    while(done<length) {
        uint64_t position=offset+done;auto data=page(object,position/object.page_bytes);
        size_t within=position%object.page_bytes,n=std::min(length-done,data->size()-within);
        std::copy_n(data->data()+within,n,result.data()+done);done+=n;
    }
    return result;
}
std::string Engine::metrics(const Object& object) {
    std::string last;{std::lock_guard<std::mutex> l(error_mutex_);last=last_error_;}
    return Json{{"backend","native-cpp.v1"},{"object",object.id},{"bytes",object.bytes},{"page_bytes",object.page_bytes},
        {"cache_bytes",cache.budget()},{"cache_used_bytes",cache.used()},{"counters",counters.snapshot()},
        {"transport",transport.stats.snapshot()},{"last_source_error",last}}.dump()+"\n";
}

void Engine::serve(std::atomic<bool>& stop) {
    int listener=socket(AF_INET6,SOCK_STREAM|SOCK_CLOEXEC,0);if(listener<0)throw std::runtime_error("Cannot open dual-stack cache listener");
    int zero=0,one=1;setsockopt(listener,IPPROTO_IPV6,IPV6_V6ONLY,&zero,sizeof(zero));setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&one,sizeof(one));
    sockaddr_in6 address{};address.sin6_family=AF_INET6;address.sin6_port=htons(config_.at("peer_port").get<int>());
    if(bind(listener,reinterpret_cast<sockaddr*>(&address),sizeof(address))||listen(listener,64)){::close(listener);throw std::runtime_error("Cannot bind cache listener");}
    Pool clients(32,32);
    while(!stop) {
        pollfd poller{listener,POLLIN,0};if(poll(&poller,1,200)<=0)continue;
        int fd=accept4(listener,nullptr,nullptr,SOCK_CLOEXEC);if(fd<0)continue;
        if(!clients.submit([this,fd,&stop]{
            try {
                auto accepted=transport.accept(fd);auto& c=*accepted.first;const auto& peer=accepted.second;
                while(!stop) {
                    c.begin();auto line=c.line();auto headers=read_headers(c);
                    std::istringstream input(line);std::string method,path,version,extra;input>>method>>path>>version;
                    if(input>>extra||version!="HTTP/1.1"||(method!="GET"&&method!="PUT"))break;
                    size_t size=0;
                    if(headers.count("content-length")) {
                        auto text=headers.at("content-length");if(text.empty()||text.find_first_not_of("0123456789")!=std::string::npos)break;
                        size=std::stoull(text);
                    }
                    if(size>262144||(method=="GET"&&size))break;
                    auto data=c.body(size);
                    if(path.substr(0,10)!="/v1/cache/"){respond(c,404,{});continue;}
                    auto tail=path.substr(10);auto slash=tail.find('/');if(slash==std::string::npos){respond(c,404,{});continue;}
                    auto id=tail.substr(0,slash),page=tail.substr(slash+1);auto i=objects.find(id);
                    if(i==objects.end()||!i->second->allowed.count(peer)){respond(c,403,{});continue;}
                    const auto& object=*i->second;
                    if(page=="inventory"&&method=="GET"){respond(c,200,cache.inventory(id));continue;}
                    if(page.empty()||page.size()>12||page.find_first_not_of("0123456789")!=std::string::npos){respond(c,400,{});continue;}
                    auto number=std::stoull(page);if(number>=object.hashes.size()){respond(c,416,{});continue;}
                    if(method=="GET") {
                        auto hit=cache.get({id,number});if(hit){respond(c,200,*hit);counters.add("peer_pages_served");}else respond(c,404,{});
                    } else {
                        if(!object.verify(number,data)){counters.add("rejected_peer_writes");respond(c,422,{});continue;}
                        bool stored=cache.put({id,number},std::make_shared<const Bytes>(std::move(data)));
                        respond(c,stored?201:507,{});if(stored)counters.add("peer_pages_admitted");
                    }
                }
            }catch(const std::exception&){counters.add("peer_connections_closed");}
        }))::close(fd);
    }
    ::close(listener);clients.stop();
}
}
