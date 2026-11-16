#include "transport.hpp"
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <cctype>

namespace swarm {
namespace {
void tls_ok(bool ok,const char* message){if(!ok)throw IntegrityError(message);}
std::string fingerprint(SSL* ssl) {
    X509* cert=SSL_get1_peer_certificate(ssl);if(!cert)throw IntegrityError("Missing peer certificate");
    Digest d{};unsigned n=0;int ok=X509_digest(cert,EVP_sha256(),d.data(),&n);X509_free(cert);
    tls_ok(ok==1&&n==32,"Certificate digest failed");return hex(d);
}
void socket_options(int fd) {
    timeval timeout{3,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
    setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
    int one=1;setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one));
}
int dial(const Json& discovery,const std::string& name,int port,Counters& stats) {
    // Python's control plane supplies freshly discovered AF_UNSPEC/DNS-SD
    // candidates; addresses locate peers and never replace TLS authentication.
    auto now=seconds_now();auto updated=discovery.at("updated").get<uint64_t>();
    if(updated>now+5||(now>updated&&now-updated>30))throw std::runtime_error("Discovery snapshot expired");
    const auto& rows=discovery.at("hosts").at(name);
    std::vector<pollfd> sockets;std::vector<int> families;
    auto close_all=[&]{for(auto p:sockets)if(p.fd>=0)::close(p.fd);};
    for(const auto& row:rows) {
        if(sockets.size()>=32)break;
        int family=row.at("family")==6?AF_INET6:AF_INET;
        sockaddr_storage storage{};socklen_t length;
        std::string address=row.at("address");
        if(family==AF_INET6) {
            auto* a=reinterpret_cast<sockaddr_in6*>(&storage);a->sin6_family=family;a->sin6_port=htons(port);
            a->sin6_scope_id=row.value("scope",0u);length=sizeof(*a);
            if(inet_pton(family,address.c_str(),&a->sin6_addr)!=1)continue;
            if(IN6_IS_ADDR_LINKLOCAL(&a->sin6_addr)&&!a->sin6_scope_id)continue;
        } else {
            auto* a=reinterpret_cast<sockaddr_in*>(&storage);a->sin_family=family;a->sin_port=htons(port);length=sizeof(*a);
            if(inet_pton(family,address.c_str(),&a->sin_addr)!=1)continue;
        }
        int fd=socket(family,SOCK_STREAM|SOCK_CLOEXEC,262 /* IPPROTO_MPTCP */);
        if(fd<0)fd=socket(family,SOCK_STREAM|SOCK_CLOEXEC,IPPROTO_TCP);
        if(fd<0)continue;
        fcntl(fd,F_SETFL,O_NONBLOCK);socket_options(fd);
        int rc=::connect(fd,reinterpret_cast<sockaddr*>(&storage),length);
        if(rc<0&&errno!=EINPROGRESS){::close(fd);continue;}
        sockets.push_back({fd,POLLOUT,0});families.push_back(family);
    }
    auto deadline=Clock::now()+std::chrono::seconds(3);
    while(!sockets.empty()&&Clock::now()<deadline) {
        if(std::none_of(sockets.begin(),sockets.end(),[](const pollfd& p){return p.fd>=0;}))break;
        int remaining=std::chrono::duration_cast<std::chrono::milliseconds>(deadline-Clock::now()).count();
        if(poll(sockets.data(),sockets.size(),std::max(1,remaining))<=0)break;
        for(size_t i=0;i<sockets.size();i++)if(sockets[i].fd>=0&&sockets[i].revents) {
            int error=0;socklen_t n=sizeof(error);getsockopt(sockets[i].fd,SOL_SOCKET,SO_ERROR,&error,&n);
            if(!error) {
                int fd=sockets[i].fd;sockets[i].fd=-1;close_all();fcntl(fd,F_SETFL,0);
                stats.add(families[i]==AF_INET6?"ipv6_socket_connections":"ipv4_socket_connections");return fd;
            }
            ::close(sockets[i].fd);sockets[i].fd=-1;
        }
    }
    close_all();throw std::runtime_error("No reachable discovered address");
}
}

Connection::~Connection(){if(ssl_)SSL_free(ssl_);if(fd_>=0)::close(fd_);}
void Connection::write(const unsigned char* p,size_t n) {
    while(n) {
        if(Clock::now()>deadline_)throw std::runtime_error("TLS operation deadline");
        int count=SSL_write(ssl_,p,std::min<size_t>(n,65536));
        if(count<=0)throw std::runtime_error("TLS write interrupted");
        p+=count;n-=count;
    }
}
std::string Connection::line() {
    while(true) {
        auto end=pending_.find("\r\n");
        if(end!=std::string::npos){auto line=pending_.substr(0,end);pending_.erase(0,end+2);return line;}
        if(pending_.size()>16384||Clock::now()>deadline_)throw std::runtime_error("HTTP header bound exceeded");
        char data[16384];int n=SSL_read(ssl_,data,sizeof(data));if(n<=0)throw std::runtime_error("TLS read interrupted");
        pending_.append(data,n);
    }
}
void Connection::quick_ack(){int one=1;setsockopt(fd_,IPPROTO_TCP,TCP_QUICKACK,&one,sizeof(one));}
Bytes Connection::body(size_t n) {
    if(n>1024*1024)throw std::runtime_error("HTTP body bound exceeded");
    Bytes data(n);size_t have=std::min(n,pending_.size());std::copy_n(pending_.data(),have,data.data());pending_.erase(0,have);
    quick_ack();
    while(have<n) {
        if(Clock::now()>deadline_)throw std::runtime_error("TLS body deadline");
        int count=SSL_read(ssl_,data.data()+have,n-have);if(count<=0)throw std::runtime_error("TLS body interrupted");have+=count;quick_ack();
    }
    return data;
}
std::unordered_map<std::string,std::string> read_headers(Connection& c) {
    std::unordered_map<std::string,std::string> headers;size_t bytes=0;
    while(true){auto line=c.line();bytes+=line.size();if(bytes>16384)throw std::runtime_error("HTTP headers too large");if(line.empty())break;
        auto p=line.find(':');if(p==std::string::npos)throw std::runtime_error("Malformed HTTP header");
        auto key=line.substr(0,p);std::transform(key.begin(),key.end(),key.begin(),[](unsigned char x){return std::tolower(x);});
        auto start=line.find_first_not_of(" \t",p+1);auto value=start==std::string::npos?"":line.substr(start);
        if(!headers.emplace(key,value).second)throw std::runtime_error("Duplicate HTTP header");
    }
    if(headers.count("transfer-encoding"))throw std::runtime_error("Chunked transfers are not supported");
    return headers;
}

Transport::Transport(const Json& config):config_({{"discovery",config.at("discovery")}}),enrollment_(read_json(config.at("tls_root").get<std::string>()+"/config.json")) {
    std::ifstream machine("/etc/machine-id");std::stringstream content;content<<machine.rdbuf();auto value=content.str();
    if(hex(hash(reinterpret_cast<const unsigned char*>(value.data()),value.size()))!=enrollment_.at("machine"))throw IntegrityError("Local machine identity differs");
    auto root=config.at("tls_root").get<std::string>();
    auto make=[&](bool server){
        SSL_CTX* ctx=SSL_CTX_new(TLS_method());if(!ctx)throw std::runtime_error("TLS context unavailable");
        if(SSL_CTX_set_min_proto_version(ctx,TLS1_3_VERSION)!=1||SSL_CTX_load_verify_locations(ctx,(root+"/ca.crt").c_str(),nullptr)!=1
           ||SSL_CTX_use_certificate_chain_file(ctx,(root+"/identity.crt").c_str())!=1
           ||SSL_CTX_use_PrivateKey_file(ctx,(root+"/identity.key").c_str(),SSL_FILETYPE_PEM)!=1||SSL_CTX_check_private_key(ctx)!=1){SSL_CTX_free(ctx);throw IntegrityError("Invalid enrolled TLS credentials");}
        SSL_CTX_set_verify(ctx,SSL_VERIFY_PEER|(server?SSL_VERIFY_FAIL_IF_NO_PEER_CERT:0),nullptr);return ctx;
    };
    client_=make(false);try{server_=make(true);}catch(...){SSL_CTX_free(client_);throw;}
}
Transport::~Transport(){idle_.clear();SSL_CTX_free(client_);SSL_CTX_free(server_);}
std::unique_ptr<Connection> Transport::connect(const std::string& peer,int port) {
    const auto& row=enrollment_.at("peers").at(peer);std::string hostname=row.at("address");
    if(peer.size()<7||peer.substr(peer.size()-6)!=".local"||hostname.size()<7||hostname.substr(hostname.size()-6)!=".local")throw IntegrityError("Use enrolled hostnames");
    int fd=dial(read_json(config_.at("discovery")),peer,port,stats);SSL* ssl=SSL_new(client_);
    auto connection=std::make_unique<Connection>(fd,ssl);
    tls_ok(ssl&&SSL_set_fd(ssl,fd)==1&&SSL_set1_host(ssl,hostname.c_str())==1&&SSL_set_tlsext_host_name(ssl,hostname.c_str())==1,"TLS setup failed");
    int connected=SSL_connect(ssl);
    tls_ok(SSL_get_verify_result(ssl)==X509_V_OK,"TLS certificate verification failed");
    if(connected!=1)throw std::runtime_error("TLS handshake interrupted");
    tls_ok(fingerprint(ssl)==row.at("certificate_sha256"),"Peer certificate pin differs");
    sockaddr_storage remote{};socklen_t length=sizeof(remote);getpeername(fd,reinterpret_cast<sockaddr*>(&remote),&length);
    stats.add(remote.ss_family==AF_INET6?"ipv6_connections":"ipv4_connections");return connection;
}
Response Transport::request(const std::string& peer,int port,const std::string& path,const std::unordered_map<std::string,std::string>& headers,const Bytes& body,bool put) {
    if(path.find_first_of("\r\n ")!=std::string::npos)throw IntegrityError("Invalid request target");
    std::string key=peer+":"+std::to_string(port);
    for(int attempt=0;attempt<2;attempt++) {
        std::unique_ptr<Connection> c;
        {std::lock_guard<std::mutex> l(mutex_);auto& pool=idle_[key];if(!pool.empty()){c=std::move(pool.back());pool.pop_back();}}
        try {
            if(!c)c=connect(peer,port);
            c->begin();
            std::string head=(put?"PUT ":"GET ")+path+" HTTP/1.1\r\nHost: "+peer+"\r\nContent-Length: "+std::to_string(body.size())+"\r\n";
            for(const auto& h:headers){if(h.first.find_first_of("\r\n")!=std::string::npos||h.second.find_first_of("\r\n")!=std::string::npos)throw IntegrityError("Invalid request header");head+=h.first+": "+h.second+"\r\n";}
            c->write(head+"\r\n");if(!body.empty())c->write(body.data(),body.size());
            auto status=c->line();if(status.substr(0,9)!="HTTP/1.1 "&&status.substr(0,9)!="HTTP/1.0 ")throw std::runtime_error("Invalid HTTP status");
            Response result;result.status=std::stoi(status.substr(9,3));result.headers=read_headers(*c);
            auto len=result.headers.at("content-length");if(len.empty()||len.find_first_not_of("0123456789")!=std::string::npos)throw std::runtime_error("Invalid response length");
            result.body=c->body(std::stoull(len));
            if(result.headers["connection"]!="close") {std::lock_guard<std::mutex> l(mutex_);if(idle_[key].size()<8)idle_[key].push_back(std::move(c));}
            return result;
        } catch(const IntegrityError&){throw;} catch(const std::exception&){stats.add("transport_retries");if(attempt)throw;}
    }
    throw std::runtime_error("Transport unavailable");
}
std::pair<std::unique_ptr<Connection>,std::string> Transport::accept(int fd) {
    socket_options(fd);SSL* ssl=SSL_new(server_);auto c=std::make_unique<Connection>(fd,ssl);
    tls_ok(ssl&&SSL_set_fd(ssl,fd)==1&&SSL_accept(ssl)==1&&SSL_get_verify_result(ssl)==X509_V_OK,"Peer TLS authentication failed");
    auto pin=fingerprint(ssl);
    for(auto i=enrollment_.at("peers").begin();i!=enrollment_.at("peers").end();++i)
        if(i.value().at("certificate_sha256")==pin)return {std::move(c),i.key()};
    throw IntegrityError("Unenrolled peer certificate");
}
void respond(Connection& c,int status,const Bytes& data) {
    c.write("HTTP/1.1 "+std::to_string(status)+" Result\r\nContent-Length: "+std::to_string(data.size())+"\r\nContent-Type: application/octet-stream\r\n\r\n");
    if(!data.empty())c.write(data.data(),data.size());
}
}
