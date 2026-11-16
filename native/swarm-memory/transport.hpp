#pragma once
#include "common.hpp"
#include <openssl/ssl.h>

namespace swarm {
struct Response {int status=0;std::unordered_map<std::string,std::string> headers;Bytes body;};
class Connection {
    int fd_=-1; SSL* ssl_=nullptr; std::string pending_;
    Clock::time_point deadline_=Clock::now()+std::chrono::seconds(10);
public:
    Connection(int fd,SSL* ssl):fd_(fd),ssl_(ssl){}
    ~Connection();
    Connection(const Connection&)=delete;
    void begin(){deadline_=Clock::now()+std::chrono::seconds(10);}
    void write(const unsigned char* p,size_t n);
    void write(const std::string& s){write(reinterpret_cast<const unsigned char*>(s.data()),s.size());}
    std::string line();
    Bytes body(size_t n);
    void quick_ack();
};
class Transport {
    Json config_,enrollment_;
    SSL_CTX *client_=nullptr,*server_=nullptr;
    std::mutex mutex_;
    std::unordered_map<std::string,std::vector<std::unique_ptr<Connection>>> idle_;
    std::unique_ptr<Connection> connect(const std::string& peer,int port);
public:
    Counters stats;
    explicit Transport(const Json& config);
    ~Transport();
    Response request(const std::string& peer,int port,const std::string& path,
                     const std::unordered_map<std::string,std::string>& headers={},const Bytes& body={},bool put=false);
    std::pair<std::unique_ptr<Connection>,std::string> accept(int fd);
    const Json& enrollment()const{return enrollment_;}
    std::string name()const{return enrollment_.at("name");}
};
std::unordered_map<std::string,std::string> read_headers(Connection& c);
void respond(Connection& c,int status,const Bytes& data);
}
