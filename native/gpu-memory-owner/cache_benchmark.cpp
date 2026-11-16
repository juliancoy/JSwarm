#include <fstream>
// Experimental global grouping; production owner/default shader remain unchanged.
#define main resident_benchmark_main
#include "vram_benchmark.cpp"
#undef main
struct CacheBenchmark : Benchmark {
 VkShaderModule module=VK_NULL_HANDLE; VkPipeline grouped=VK_NULL_HANDLE;
 CacheBenchmark(const char* gather,const char* init,const char* group):Benchmark(gather,init){
  std::ifstream file(group,std::ios::binary|std::ios::ate);
  if(!file)throw std::runtime_error("group shader unavailable");
  auto n=file.tellg();if(n<=0||size_t(n)%4)throw std::runtime_error("invalid group shader");
  std::vector<uint32_t> code(size_t(n)/4);file.seekg(0);file.read(reinterpret_cast<char*>(code.data()),n);
  if(!file)throw std::runtime_error("truncated group shader");
  VkShaderModuleCreateInfo si{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};si.codeSize=size_t(n);si.pCode=code.data();check(vkCreateShaderModule(owner.device,&si,nullptr,&module),"group module");
  VkComputePipelineCreateInfo pi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};pi.layout=owner.layout;pi.stage.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;pi.stage.stage=VK_SHADER_STAGE_COMPUTE_BIT;pi.stage.module=module;pi.stage.pName="main";
  check(vkCreateComputePipelines(owner.device,VK_NULL_HANDLE,1,&pi,nullptr,&grouped),"group pipeline");
 }
 ~CacheBenchmark(){vkDeviceWaitIdle(owner.device);if(grouped)vkDestroyPipeline(owner.device,grouped,nullptr);if(module)vkDestroyShaderModule(owner.device,module,nullptr);}
 void sync(){barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT);}
 void dispatch(uint32_t mode,uint32_t count){bind(grouped);uint32_t pc[4]={count,uint32_t(segment_size/4),mode,object_words};vkCmdPushConstants(owner.command,owner.layout,VK_SHADER_STAGE_COMPUTE_BIT,0,16,pc);vkCmdDispatch(owner.command,mode==1?1:(count+255)/256,1,1);sync();}
 void group(uint32_t count){
  barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_WRITE_BIT);
  vkCmdFillBuffer(owner.command,owner.io.buffer,uint64_t(count)*20,1024,0);
  barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT);
  dispatch(0,count);dispatch(1,count);dispatch(2,count);
 }
 double timed(uint32_t count,uint32_t repeats,bool include_group){
  owner.begin();barrier(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT);
  vkCmdResetQueryPool(owner.command,timestamps,0,2);vkCmdWriteTimestamp(owner.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,timestamps,0);
  for(uint32_t i=0;i<repeats;++i){if(include_group)group(count);dispatch(3,count);dispatch(4,count);}
  vkCmdWriteTimestamp(owner.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,timestamps,1);owner.submit();
  uint64_t ticks[2];check(vkGetQueryPoolResults(owner.device,timestamps,0,2,sizeof(ticks),ticks,8,VK_QUERY_RESULT_64_BIT|VK_QUERY_RESULT_WAIT_BIT),"group timestamps");
  uint64_t mask=owner.timestamp_bits==64?UINT64_MAX:((1ull<<owner.timestamp_bits)-1);
  return double((ticks[1]-ticks[0])&mask)*owner.properties.limits.timestampPeriod/1e9;
 }
 void check_edges(){
  for(uint32_t count:{1u,65u,4097u})for(unsigned pattern=0;pattern<3;++pattern){
   std::vector<uint32_t> input(count*2,0xdeadbeefu);
   for(uint32_t i=0;i<count;++i)input[i]=pattern==0?17u:pattern==1?object_words-1-i:(i%3==0?UINT32_MAX:i%3==1?object_words:i);
   owner.begin();barrier(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_TRANSFER_WRITE_BIT);
   vkCmdUpdateBuffer(owner.command,owner.io.buffer,0,input.size()*4,input.data());
   barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT);owner.submit();
   timed(count,1,true);
   owner.begin();barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
   VkBufferCopy copy{uint64_t(count)*4,0,uint64_t(count)*4};vkCmdCopyBuffer(owner.command,owner.io.buffer,readback.buffer,1,&copy);
   barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_HOST_READ_BIT);owner.submit();
   auto* output=static_cast<uint32_t*>(readback.mapped);
   for(uint32_t i=0;i<count;++i){uint32_t index=input[i],expected=index<object_words?((index*2654435761u)^0x5a5aa5a5u):0u;if(output[i]!=expected)throw std::runtime_error("cache grouping edge/order/bounds mismatch");}
  }
  std::cerr<<"Cache grouping duplicate, reverse-order and invalid-index edge checks passed\n";
 }
 void row(uint32_t count){
  prepare(2,count);timed(count,1,true);verify_gather(count);
  for(const char* mode:{"direct","group-every-batch","grouped-reuse"}){
   std::cout<<"{\"count\":"<<count<<",\"mode\":\""<<mode<<"\",\"repetitions\":8,\"gpu_seconds\":[";
   for(unsigned sample=0;sample<5;++sample){double seconds=std::string(mode)=="direct"?run(false,false,count,8):timed(count,8,std::string(mode)=="group-every-batch");if(sample)std::cout<<",";std::cout<<seconds;}
   verify_gather(count);std::cout<<"],\"verified\":true}"<<std::endl;
  }
 }
};
int main(int argc,char** argv){try{
 if(argc!=4)throw std::runtime_error("usage: cache-benchmark GATHER SETUP GROUP");
 CacheBenchmark b(argv[1],argv[2],argv[3]);b.initialize();b.check_edges();
 for(uint32_t count:{1u,63u,64u,65u,4095u,65536u,1048576u,2097152u})b.row(count);
 return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}}
