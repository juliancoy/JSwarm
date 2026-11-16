// Standalone bounded benchmark. Reuses allocation/device helpers, never starts
// or changes the production GPU owner. All timed data remain DEVICE_LOCAL.
#include "owner.cpp"
#include <iostream>

namespace {
constexpr uint64_t reference_bytes=256ull*1024*1024;
constexpr uint64_t segment_size=reference_bytes/4;
constexpr uint32_t object_words=reference_bytes/4;
struct Benchmark {
 Owner owner;
 VkPipeline setup=VK_NULL_HANDLE;
 VkShaderModule setup_shader=VK_NULL_HANDLE;
 VkQueryPool timestamps=VK_NULL_HANDLE;
 Buffer readback;
 Benchmark(const char* gather,const char* initialize){
  owner.initialize(gather,"",reference_bytes,segment_size);
  if(!owner.timestamp_bits)throw std::runtime_error("queue timestamps unavailable");
  owner.destroy(owner.io);
  owner.io=owner.allocate(reference_bytes,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
  for(unsigned i=0;i<4;++i)owner.reference.push_back(owner.allocate(segment_size,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT));
  owner.object_bytes=reference_bytes;owner.segment_bytes=segment_size;
  std::array<VkDescriptorBufferInfo,5> infos{};std::array<VkWriteDescriptorSet,5> writes{};
  for(unsigned i=0;i<5;++i){auto& b=i==4?owner.io:owner.reference[i];infos[i]={b.buffer,0,b.size};writes[i].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[i].dstSet=owner.set;writes[i].dstBinding=i;writes[i].descriptorCount=1;writes[i].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;writes[i].pBufferInfo=&infos[i];}
  vkUpdateDescriptorSets(owner.device,writes.size(),writes.data(),0,nullptr);
  auto close_file=[](FILE* handle){if(handle)fclose(handle);};
  std::unique_ptr<FILE,decltype(close_file)> file(fopen(initialize,"rb"),close_file);
  if(!file)throw std::runtime_error("setup shader unavailable");
  fseek(file.get(),0,SEEK_END);long size=ftell(file.get());rewind(file.get());
  if(size<=0||size%4)throw std::runtime_error("invalid setup shader");
  std::vector<uint32_t> code(size/4);if(fread(code.data(),1,size,file.get())!=size_t(size))throw std::runtime_error("setup shader truncated");
  VkShaderModuleCreateInfo si{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};si.codeSize=size;si.pCode=code.data();check(vkCreateShaderModule(owner.device,&si,nullptr,&setup_shader),"setup shader module");
  VkComputePipelineCreateInfo pi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};pi.layout=owner.layout;pi.stage.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;pi.stage.stage=VK_SHADER_STAGE_COMPUTE_BIT;pi.stage.module=setup_shader;pi.stage.pName="main";check(vkCreateComputePipelines(owner.device,VK_NULL_HANDLE,1,&pi,nullptr,&setup),"setup pipeline");
  VkQueryPoolCreateInfo qi{VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO};qi.queryType=VK_QUERY_TYPE_TIMESTAMP;qi.queryCount=2;check(vkCreateQueryPool(owner.device,&qi,nullptr,&timestamps),"resident timestamps");
  readback=owner.allocate(16ull*1024*1024,VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
 }
 ~Benchmark(){if(owner.device){vkDeviceWaitIdle(owner.device);owner.destroy(readback);if(timestamps)vkDestroyQueryPool(owner.device,timestamps,nullptr);if(setup)vkDestroyPipeline(owner.device,setup,nullptr);if(setup_shader)vkDestroyShaderModule(owner.device,setup_shader,nullptr);}}
 void barrier(VkPipelineStageFlags from,VkPipelineStageFlags to,VkAccessFlags src,VkAccessFlags dst){VkMemoryBarrier b{VK_STRUCTURE_TYPE_MEMORY_BARRIER};b.srcAccessMask=src;b.dstAccessMask=dst;vkCmdPipelineBarrier(owner.command,from,to,0,1,&b,0,nullptr,0,nullptr);}
 void bind(VkPipeline pipeline){vkCmdBindPipeline(owner.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);vkCmdBindDescriptorSets(owner.command,VK_PIPELINE_BIND_POINT_COMPUTE,owner.layout,0,1,&owner.set,0,nullptr);}
 void setup_dispatch(uint32_t mode,uint32_t count){
  bind(setup);uint32_t pc[4]={mode,count,uint32_t(segment_size/4),object_words};vkCmdPushConstants(owner.command,owner.layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(pc),pc);
  uint64_t groups=(uint64_t(count)+255)/256;uint32_t x=std::min<uint64_t>(groups,owner.properties.limits.maxComputeWorkGroupCount[0]),y=(groups+x-1)/x;
  if(y>owner.properties.limits.maxComputeWorkGroupCount[1])throw std::runtime_error("setup grid exceeds device limits");
  vkCmdDispatch(owner.command,x,y,1);
 }
 void initialize(){owner.begin();setup_dispatch(0,object_words);barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT|VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_TRANSFER_READ_BIT);owner.submit();}
 uint64_t allocation_bytes(){uint64_t bytes=owner.io.allocation;for(auto& b:owner.reference)bytes+=b.allocation;return bytes;}
 double run(bool bulk,bool compute_stream,uint32_t count,uint32_t repetitions){
  owner.begin();
  barrier(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT);
  vkCmdResetQueryPool(owner.command,timestamps,0,2);
  vkCmdWriteTimestamp(owner.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,timestamps,0);
  for(uint32_t repeat=0;repeat<repetitions;++repeat){
   if(bulk){
    for(unsigned i=0;i<4;++i){VkBufferCopy copy{0,i*segment_size,segment_size};vkCmdCopyBuffer(owner.command,owner.reference[i].buffer,owner.io.buffer,1,&copy);}
    barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_WRITE_BIT);
   }else{
    if(compute_stream)setup_dispatch(3,object_words);
    else{
     bind(owner.pipeline);uint32_t pc[4]={count,uint32_t(segment_size/4),count,object_words};vkCmdPushConstants(owner.command,owner.layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(pc),pc);
     if((count+63)/64>owner.properties.limits.maxComputeWorkGroupCount[0])throw std::runtime_error("gather grid exceeds device limit");
     vkCmdDispatch(owner.command,(count+63)/64,1,1);
    }
    // Preserve repeated output writes; no host synchronization between dispatches.
    barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_SHADER_WRITE_BIT);
   }
  }
  vkCmdWriteTimestamp(owner.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,timestamps,1);
  owner.submit();uint64_t ticks[2];check(vkGetQueryPoolResults(owner.device,timestamps,0,2,sizeof(ticks),ticks,8,VK_QUERY_RESULT_64_BIT|VK_QUERY_RESULT_WAIT_BIT),"read resident timing");
  uint64_t mask=owner.timestamp_bits==64?UINT64_MAX:((1ull<<owner.timestamp_bits)-1);
  return double((ticks[1]-ticks[0])&mask)*owner.properties.limits.timestampPeriod/1e9;
 }
 void prepare(uint32_t mode,uint32_t count){owner.begin();setup_dispatch(mode,count);barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_SHADER_WRITE_BIT);owner.submit();}
 void verify_gather(uint32_t count){
  const uint64_t bytes=count*8ull;if(bytes>readback.size)throw std::runtime_error("verification exceeds readback budget");
  owner.begin();barrier(VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);VkBufferCopy copy{0,0,bytes};vkCmdCopyBuffer(owner.command,owner.io.buffer,readback.buffer,1,&copy);barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_HOST_READ_BIT);owner.submit();
  auto* values=static_cast<uint32_t*>(readback.mapped);
  for(uint32_t i=0;i<count;++i){uint32_t index=values[i];if(index>=object_words||values[count+i]!=((index*2654435761u)^0x5a5aa5a5u))throw std::runtime_error("resident gather output mismatch");}
 }
 void verify_stream(){
  // Read contiguous windows at every segment boundary outside the timing region.
  std::vector<uint64_t> positions={0,segment_size-4096,segment_size,2*segment_size-4096,2*segment_size,3*segment_size-4096,3*segment_size,reference_bytes-4096};
  owner.begin();barrier(VK_PIPELINE_STAGE_TRANSFER_BIT|VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_ACCESS_TRANSFER_WRITE_BIT|VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
  for(size_t i=0;i<positions.size();++i){VkBufferCopy copy{positions[i],i*4096,4096};vkCmdCopyBuffer(owner.command,owner.io.buffer,readback.buffer,1,&copy);}
  barrier(VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_HOST_READ_BIT);owner.submit();
  auto* values=static_cast<uint32_t*>(readback.mapped);for(size_t i=0;i<positions.size();++i)for(unsigned j=0;j<1024;++j)if(values[i*1024+j]!=(((uint32_t(positions[i]/4)+j)*2654435761u)^0x5a5aa5a5u))throw std::runtime_error("stream output mismatch");
 }
 void row(const char* name,uint32_t count,bool bulk,bool compute_stream){
  constexpr uint32_t repeats=32;
  if(!bulk&&!compute_stream){prepare(std::string(name)=="random-gather"?2:1,count);}
  run(bulk,compute_stream,count,3); // Warmup is not one of the measured samples.
  if(bulk||compute_stream)verify_stream();else verify_gather(count);
  std::cout<<"{\"case\":\""<<name<<"\",\"count\":"<<count<<",\"repetitions\":"<<repeats<<",\"gpu_seconds\":[";
  for(unsigned sample=0;sample<5;++sample){double seconds=run(bulk,compute_stream,count,repeats);if(sample)std::cout<<",";std::cout<<seconds;}
  if(bulk||compute_stream)verify_stream();else verify_gather(count);
  std::cout<<"],\"logical_bytes_per_iteration\":"<<(bulk||compute_stream?reference_bytes*2:uint64_t(count)*12)<<",\"verified\":true}"<<std::endl;
 }
};
}
int main(int argc,char** argv){
 try{
  if(argc!=3)throw std::runtime_error("usage: vram-benchmark GATHER_SPV SETUP_SPV");
  Benchmark bench(argv[1],argv[2]);bench.initialize();
  std::cout<<"{\"device\":\""<<bench.owner.name<<"\",\"device_uuid\":\""<<bench.owner.device_uuid<<"\",\"device_allocation_bytes\":"<<bench.allocation_bytes()<<",\"reference_bytes\":"<<reference_bytes<<",\"host_readback_bytes\":"<<bench.readback.size<<",\"timestamp_period_ns\":"<<bench.owner.properties.limits.timestampPeriod<<",\"timed_host_payload_copies\":0,\"timed_host_roundtrips_between_dispatches\":0}"<<std::endl;
  bench.row("transfer-copy",object_words,true,false);
  bench.row("compute-copy",object_words,false,true);
  for(uint32_t count:{65536u,1048576u,2097152u}){
   bench.row("sequential-gather",count,false,false);
   bench.row("random-gather",count,false,false);
  }
  return 0;
 }catch(const std::exception& e){std::cerr<<e.what()<<std::endl;return 1;}
}
