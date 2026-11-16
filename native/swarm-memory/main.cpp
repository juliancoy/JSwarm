#define FUSE_USE_VERSION 31
#include <fuse_lowlevel.h>
#include "engine.hpp"
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <unistd.h>

namespace {
std::atomic<bool> stopping{false};
void stop(int){stopping=true;}
struct Mount {
    swarm::Engine* engine;std::shared_ptr<swarm::Object> object;fuse_session* session=nullptr;std::thread loop;
    ~Mount(){
        if(session){fuse_session_exit(session);fuse_session_unmount(session);}
        if(loop.joinable())loop.join();
        engine->reads.stop();
        if(session)fuse_session_destroy(session);
    }
};
Mount& mount(fuse_req_t req){return *static_cast<Mount*>(fuse_req_userdata(req));}
struct stat attributes(Mount& m,fuse_ino_t ino) {
    struct stat st{};st.st_ino=ino;st.st_uid=getuid();st.st_gid=getgid();st.st_blksize=65536;
    if(ino==1){st.st_mode=S_IFDIR|0500;st.st_nlink=2;}
    else if(ino==2){st.st_mode=S_IFREG|0400;st.st_nlink=1;st.st_size=m.object->bytes;st.st_blocks=(st.st_size+511)/512;}
    else if(ino==3){st.st_mode=S_IFREG|0400;st.st_nlink=1;st.st_size=m.engine->metrics(*m.object).size();}
    return st;
}
void init(void*,fuse_conn_info* conn){conn->max_read=128*1024;conn->max_background=32;conn->congestion_threshold=24;}
void lookup(fuse_req_t req,fuse_ino_t parent,const char* name) {
    fuse_ino_t ino=parent==1?(std::strcmp(name,"object")==0?2:std::strcmp(name,"metrics.json")==0?3:0):0;
    if(!ino){fuse_reply_err(req,ENOENT);return;}
    fuse_entry_param entry{};entry.ino=ino;entry.attr=attributes(mount(req),ino);entry.attr_timeout=ino==2?60:0;entry.entry_timeout=60;
    fuse_reply_entry(req,&entry);
}
void getattr(fuse_req_t req,fuse_ino_t ino,fuse_file_info*) {
    if(ino<1||ino>3){fuse_reply_err(req,ENOENT);return;}auto st=attributes(mount(req),ino);fuse_reply_attr(req,&st,ino==2?60:0);
}
void readdir(fuse_req_t req,fuse_ino_t ino,size_t size,off_t offset,fuse_file_info*) {
    if(ino!=1){fuse_reply_err(req,ENOTDIR);return;}std::string data;
    for(auto pair:{std::make_pair(".",1),{"..",1},{"object",2},{"metrics.json",3}}) {
        auto st=attributes(mount(req),pair.second);size_t n=fuse_add_direntry(req,nullptr,0,pair.first,&st,0),at=data.size();
        data.resize(at+n);fuse_add_direntry(req,data.data()+at,n,pair.first,&st,at+n);
    }
    if(offset<0){fuse_reply_err(req,EINVAL);return;}
    if(static_cast<size_t>(offset)>=data.size()){fuse_reply_buf(req,nullptr,0);return;}
    fuse_reply_buf(req,data.data()+offset,std::min(size,data.size()-offset));
}
void open(fuse_req_t req,fuse_ino_t ino,fuse_file_info* fi) {
    if(ino!=2&&ino!=3){fuse_reply_err(req,ENOENT);return;}
    if((fi->flags&O_ACCMODE)!=O_RDONLY){fuse_reply_err(req,EROFS);return;}
    fi->keep_cache=ino==2;fi->direct_io=ino==3;fuse_reply_open(req,fi);
}
void read(fuse_req_t req,fuse_ino_t ino,size_t size,off_t offset,fuse_file_info*) {
    auto& m=mount(req);
    if(offset<0||size>1024*1024){fuse_reply_err(req,EINVAL);return;}
    if(ino==3) {
        auto text=m.engine->metrics(*m.object);size_t at=std::min<uint64_t>(offset,text.size());
        fuse_reply_buf(req,text.data()+at,std::min(size,text.size()-at));return;
    }
    if(ino!=2){fuse_reply_err(req,ENOENT);return;}
    if(!m.engine->reads.submit([req,size,offset,&m]{
        try {auto data=m.engine->read(*m.object,offset,size);fuse_reply_buf(req,reinterpret_cast<const char*>(data.data()),data.size());}
        catch(const std::exception& error){m.engine->error(error.what());std::cerr<<"Object read failed at "<<offset<<": "<<error.what()<<'\n';fuse_reply_err(req,EIO);}
    }))fuse_reply_err(req,EAGAIN);
}
}

int main(int argc,char** argv) {
    std::signal(SIGPIPE,SIG_IGN);std::signal(SIGINT,stop);std::signal(SIGTERM,stop);
    try {
        if(argc<3||std::string(argv[1])!="--config")throw std::runtime_error("Usage: swarm-memory --config FILE [--read OBJECT OFFSET LENGTH | --serve-only]");
        swarm::Engine engine(swarm::read_json(argv[2]));
        if(argc==7&&std::string(argv[3])=="--read") {
            auto object=engine.objects.at(argv[4]);auto data=engine.read(*object,std::stoull(argv[5]),std::stoull(argv[6]));
            std::cout.write(reinterpret_cast<const char*>(data.data()),data.size());
            std::cerr<<engine.metrics(*object);return 0;
        }
        std::vector<std::unique_ptr<Mount>> mounts;
        if(!(argc==4&&std::string(argv[3])=="--serve-only")) {
            for(auto& pair:engine.objects) {
                auto m=std::make_unique<Mount>();m->engine=&engine;m->object=pair.second;
                std::string options="ro,default_permissions,max_read=131072,fsname=swarm-memory-native";
                if(m->object->allow_other)options+=",allow_other";
                else if(m->object->allow_root)options+=",allow_root";
                char name[]="swarm-memory",flag[]="-o";char* arguments[]={name,flag,options.data()};fuse_args args=FUSE_ARGS_INIT(3,arguments);
                fuse_lowlevel_ops ops{};ops.init=init;ops.lookup=lookup;ops.getattr=getattr;ops.readdir=readdir;ops.open=open;ops.read=read;
                m->session=fuse_session_new(&args,&ops,sizeof(ops),m.get());fuse_opt_free_args(&args);
                if(!m->session||fuse_session_mount(m->session,m->object->mountpoint.c_str())!=0)throw std::runtime_error("Cannot mount native object");
                auto* state=m.get();m->loop=std::thread([state]{fuse_session_loop(state->session);});mounts.push_back(std::move(m));
            }
        }
        // Listener failure is propagated to main, and all mounted sessions are
        // drained before their userdata or the shared cache can be destroyed.
        std::exception_ptr failure;
        std::thread server([&]{try{engine.serve(stopping);}catch(...){failure=std::current_exception();stopping=true;}});
        while(!stopping)std::this_thread::sleep_for(std::chrono::milliseconds(100));
        for(auto& m:mounts){fuse_session_exit(m->session);fuse_session_unmount(m->session);}
        for(auto& m:mounts)if(m->loop.joinable())m->loop.join();
        engine.reads.stop();server.join();
        for(auto& m:mounts){fuse_session_destroy(m->session);m->session=nullptr;}
        if(failure)std::rethrow_exception(failure);
    }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
    return 0;
}
