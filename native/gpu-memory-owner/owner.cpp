// One immutable device-local reference, shared by all clients of one GPU owner.
// Small queries/results use coherent mapped host memory, with no explicit
// vkCmdCopyBuffer for query upload or result readback. This is not RDMA.
#include <vulkan/vulkan.h>
#include <openssl/evp.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <memory>
#include <mutex>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <poll.h>
#include <sys/socket.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <linux/memfd.h>

namespace {
constexpr uint32_t max_queries=65536;
thread_local std::string last_error;
void check(VkResult r,const char* why){if(r!=VK_SUCCESS)throw std::runtime_error(std::string(why)+": "+std::to_string(r));}
std::string hex(const uint8_t* p,size_t n){static const char* d="0123456789abcdef";std::string s;for(size_t i=0;i<n;++i){s+=d[p[i]>>4];s+=d[p[i]&15];}return s;}
struct Buffer {VkBuffer buffer=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE;VkDeviceSize size=0,allocation=0;void* mapped=nullptr;int fd=-1;void* backing=nullptr;VkDeviceSize backing_size=0;};
struct Slot {
 Buffer io,device_io;uint32_t recorded_count=0; VkCommandPool pool=VK_NULL_HANDLE; VkCommandBuffer command=VK_NULL_HANDLE;
 VkQueryPool timestamps=VK_NULL_HANDLE;VkFence fence=VK_NULL_HANDLE; VkDescriptorSet set=VK_NULL_HANDLE; std::mutex busy;
};
using Clock=std::chrono::steady_clock;
uint64_t elapsed(Clock::time_point start,Clock::time_point end){return std::chrono::duration_cast<std::chrono::nanoseconds>(end-start).count();}
struct Gate {
 std::mutex lock;std::condition_variable changed;unsigned active=0,limit=4;
 struct Ticket{Gate& gate;Ticket(Gate& g):gate(g){std::unique_lock<std::mutex> own(gate.lock);gate.changed.wait(own,[&]{return gate.active<gate.limit;});++gate.active;}~Ticket(){std::lock_guard<std::mutex> own(gate.lock);--gate.active;gate.changed.notify_one();}};
};
bool control_transfer(int fd,uint8_t* bytes,size_t size,bool writing){
 size_t done=0;
 while(done<size){ssize_t n=writing?send(fd,bytes+done,size-done,MSG_NOSIGNAL):recv(fd,bytes+done,size-done,0);
  if(n>0){done+=n;continue;}if(n==0&&!writing&&!done)return false;
  if(n<0&&errno==EINTR)continue;
  if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK)){pollfd p{fd,short(writing?POLLOUT:POLLIN),0};int result;do{result=poll(&p,1,5000);}while(result<0&&errno==EINTR);if(result>0)continue;throw std::runtime_error("native control timeout or poll failure");}
  throw std::runtime_error("native control disconnected or truncated");
 }return true;
}
struct Profile {
 std::atomic<uint64_t> samples{0},total_ns{0},validation_ns{0},capacity_wait_ns{0},record_ns{0},submit_ns{0},fence_wait_ns{0},gpu_span_ns{0};
 std::string json(){return "{\"samples\":"+std::to_string(samples.load())+",\"total_ns\":"+std::to_string(total_ns.load())+",\"validation_ns\":"+std::to_string(validation_ns.load())+",\"capacity_wait_ns\":"+std::to_string(capacity_wait_ns.load())+",\"record_ns\":"+std::to_string(record_ns.load())+",\"submit_ns\":"+std::to_string(submit_ns.load())+",\"fence_wait_ns\":"+std::to_string(fence_wait_ns.load())+",\"gpu_span_ns\":"+std::to_string(gpu_span_ns.load())+"}";}
};
struct Owner {
 VkInstance instance=VK_NULL_HANDLE;VkPhysicalDevice physical=VK_NULL_HANDLE;VkDevice device=VK_NULL_HANDLE;VkQueue queue=VK_NULL_HANDLE;
 VkCommandPool pool=VK_NULL_HANDLE;VkCommandBuffer command=VK_NULL_HANDLE;VkFence fence=VK_NULL_HANDLE;
 VkDescriptorSetLayout descriptors=VK_NULL_HANDLE;VkPipelineLayout layout=VK_NULL_HANDLE;VkPipeline pipeline=VK_NULL_HANDLE;
 VkShaderModule shader=VK_NULL_HANDLE;VkDescriptorPool descriptor_pool=VK_NULL_HANDLE;VkDescriptorSet set=VK_NULL_HANDLE;
 VkPhysicalDeviceProperties properties{};VkPhysicalDeviceMemoryProperties memory_properties{};
 uint32_t family=0;uint64_t budget=0,segment_bytes=0,object_bytes=0,uploaded=0;std::atomic<uint64_t> queries{0};bool memory_budget=false,external_host=false;VkDeviceSize host_alignment=0;std::atomic<bool> failed{false};
 bool reuse_commands=true,staged_io=false;std::atomic<uint64_t> command_recordings{0},command_reuses{0},upload_copies{0},readback_copies{0};
 bool profiling=false;uint32_t timestamp_bits=0,spin_us=50;Profile profile;Gate gate;std::atomic<uint64_t> native_shared_batches{0};
 std::mutex queue_lock;std::vector<std::unique_ptr<Slot>> slots;std::atomic<unsigned> pending{0},peak_pending{0};
 std::string device_uuid,driver_uuid,name,digest,description;Buffer io;std::vector<Buffer> reference;
 void destroy(Buffer& b){if(b.mapped)vkUnmapMemory(device,b.memory);if(b.buffer)vkDestroyBuffer(device,b.buffer,nullptr);if(b.memory)vkFreeMemory(device,b.memory,nullptr);if(b.backing)munmap(b.backing,b.backing_size);if(b.fd>=0)close(b.fd);b={};}
 ~Owner(){if(device){vkDeviceWaitIdle(device);for(auto& slot:slots){destroy(slot->io);destroy(slot->device_io);if(slot->timestamps)vkDestroyQueryPool(device,slot->timestamps,nullptr);if(slot->fence)vkDestroyFence(device,slot->fence,nullptr);if(slot->pool)vkDestroyCommandPool(device,slot->pool,nullptr);}for(auto& b:reference)destroy(b);destroy(io);if(fence)vkDestroyFence(device,fence,nullptr);if(pool)vkDestroyCommandPool(device,pool,nullptr);if(pipeline)vkDestroyPipeline(device,pipeline,nullptr);if(shader)vkDestroyShaderModule(device,shader,nullptr);if(layout)vkDestroyPipelineLayout(device,layout,nullptr);if(descriptor_pool)vkDestroyDescriptorPool(device,descriptor_pool,nullptr);if(descriptors)vkDestroyDescriptorSetLayout(device,descriptors,nullptr);vkDestroyDevice(device,nullptr);}if(instance)vkDestroyInstance(instance,nullptr);}
 Buffer allocate(uint64_t size,VkBufferUsageFlags usage,VkMemoryPropertyFlags required){
  Buffer b;try{
   VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};bi.size=size;bi.usage=usage;bi.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
   check(vkCreateBuffer(device,&bi,nullptr,&b.buffer),"create buffer");b.size=size;
   VkMemoryRequirements req;vkGetBufferMemoryRequirements(device,b.buffer,&req);uint32_t selected=UINT32_MAX;int best=-1;
   for(uint32_t i=0;i<memory_properties.memoryTypeCount;++i){auto flags=memory_properties.memoryTypes[i].propertyFlags;if(!(req.memoryTypeBits&(1u<<i))||(flags&required)!=required)continue;
    int rank=(required&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)?((flags&VK_MEMORY_PROPERTY_HOST_CACHED_BIT)?4:0)+((flags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)?0:2):0;
    if(rank>best){best=rank;selected=i;}}
   if(selected==UINT32_MAX)throw std::runtime_error("required memory tier unavailable");
   if(memory_properties.memoryTypes[selected].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT){
    if(!memory_budget)throw std::runtime_error("GPU memory budget reporting required");
    VkPhysicalDeviceMemoryBudgetPropertiesEXT mb{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT};VkPhysicalDeviceMemoryProperties2 mp{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2};mp.pNext=&mb;vkGetPhysicalDeviceMemoryProperties2(physical,&mp);
    uint32_t heap=memory_properties.memoryTypes[selected].heapIndex;uint64_t reserve=256ull*1024*1024;
    if(mb.heapBudget[heap]<mb.heapUsage[heap]||mb.heapBudget[heap]-mb.heapUsage[heap]<req.size+reserve)throw std::runtime_error("GPU budget unavailable; preserve 256 MiB headroom");}
   VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};ai.allocationSize=req.size;ai.memoryTypeIndex=selected;
   check(vkAllocateMemory(device,&ai,nullptr,&b.memory),"allocate memory");b.allocation=req.size;check(vkBindBufferMemory(device,b.buffer,b.memory,0),"bind memory");
   if(required&VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)check(vkMapMemory(device,b.memory,0,size,0,&b.mapped),"map host buffer");
   return b;
  }catch(...){destroy(b);throw;}
 }
 Buffer allocate_shared(){
#if !defined(__x86_64__)
  throw std::runtime_error("shared host cache synchronization currently supports x86_64 only");
#endif
  if(!external_host)throw std::runtime_error("VK_EXT_external_memory_host unavailable; choose socket transport explicitly");
  Buffer b;
  try{
   VkPhysicalDeviceExternalBufferInfo ei{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO};ei.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;ei.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   VkExternalBufferProperties ep{VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES};vkGetPhysicalDeviceExternalBufferProperties(physical,&ei,&ep);
   if(!(ep.externalMemoryProperties.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT))throw std::runtime_error("host allocation storage buffer import unsupported");
   VkExternalMemoryBufferCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO};external.handleTypes=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};bi.pNext=&external;bi.size=max_queries*8;bi.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT;bi.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
   check(vkCreateBuffer(device,&bi,nullptr,&b.buffer),"create shared I/O buffer");b.size=bi.size;
   VkMemoryRequirements req;vkGetBufferMemoryRequirements(device,b.buffer,&req);
   uint64_t alignment=std::max<uint64_t>(host_alignment,sysconf(_SC_PAGESIZE));
   if(!alignment||(alignment&(alignment-1)))throw std::runtime_error("invalid imported host alignment");
   b.backing_size=(req.size+alignment-1)&~(alignment-1);b.allocation=b.backing_size;
   b.fd=syscall(SYS_memfd_create,"swarm-gpu-session",MFD_CLOEXEC|MFD_ALLOW_SEALING);
   if(b.fd<0||ftruncate(b.fd,b.backing_size)||fcntl(b.fd,F_ADD_SEALS,F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL)<0)throw std::runtime_error("cannot create sealed shared memory");
   size_t reserved_size=b.backing_size+alignment;
   void* reservation=mmap(nullptr,reserved_size,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
   if(reservation==MAP_FAILED)throw std::runtime_error("cannot reserve aligned shared mapping");
   uintptr_t base=reinterpret_cast<uintptr_t>(reservation),aligned=(base+alignment-1)&~(alignment-1);
   void* mapped=mmap(reinterpret_cast<void*>(aligned),b.backing_size,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_FIXED,b.fd,0);
   if(mapped==MAP_FAILED){munmap(reservation,reserved_size);throw std::runtime_error("cannot map shared memory");}
   if(aligned>base)munmap(reservation,aligned-base);
   uint64_t tail=base+reserved_size-aligned-b.backing_size;if(tail)munmap(reinterpret_cast<void*>(aligned+b.backing_size),tail);
   b.backing=mapped;
   auto get_properties=reinterpret_cast<PFN_vkGetMemoryHostPointerPropertiesEXT>(vkGetDeviceProcAddr(device,"vkGetMemoryHostPointerPropertiesEXT"));
   if(!get_properties)throw std::runtime_error("host import entry point unavailable");
   VkMemoryHostPointerPropertiesEXT hp{VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT};check(get_properties(device,VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT,b.backing,&hp),"query shared pointer memory");
   uint32_t selected=UINT32_MAX;VkMemoryPropertyFlags required=VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT|VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
   for(uint32_t i=0;i<memory_properties.memoryTypeCount;++i)if((req.memoryTypeBits&hp.memoryTypeBits&(1u<<i))&&(memory_properties.memoryTypes[i].propertyFlags&required)==required){selected=i;break;}
   if(selected==UINT32_MAX)throw std::runtime_error("coherent cached host import unavailable");
   VkImportMemoryHostPointerInfoEXT import{VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT};import.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;import.pHostPointer=b.backing;
   VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};ai.pNext=&import;ai.allocationSize=b.allocation;ai.memoryTypeIndex=selected;
   check(vkAllocateMemory(device,&ai,nullptr,&b.memory),"import shared memory");check(vkBindBufferMemory(device,b.buffer,b.memory,0),"bind shared memory");
   check(vkMapMemory(device,b.memory,0,b.size,0,&b.mapped),"map imported shared memory");
   return b;
  }catch(...){destroy(b);throw;}
 }
 void begin(){if(failed)throw std::runtime_error("GPU owner failed; restart required");check(vkResetFences(device,1,&fence),"reset fence");check(vkResetCommandBuffer(command,0),"reset commands");VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};begin.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;check(vkBeginCommandBuffer(command,&begin),"begin commands");}
 void submit(){failed=true;check(vkEndCommandBuffer(command),"end commands");VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.commandBufferCount=1;submit.pCommandBuffers=&command;check(vkQueueSubmit(queue,1,&submit,fence),"submit commands");check(vkWaitForFences(device,1,&fence,VK_TRUE,30000000000ull),"wait for owner GPU job");failed=false;}
 void initialize(const char* path,const char* wanted,uint64_t max_resident,uint64_t segment){
  if(!max_resident||segment<4096||segment>2ull*1024*1024*1024||segment%4||max_resident>segment*4)throw std::runtime_error("invalid resident/segment budgets");
  budget=max_resident;segment_bytes=segment;
  VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};app.apiVersion=VK_API_VERSION_1_1;VkInstanceCreateInfo ci{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};ci.pApplicationInfo=&app;check(vkCreateInstance(&ci,nullptr,&instance),"create Vulkan instance");
  uint32_t count=0;check(vkEnumeratePhysicalDevices(instance,&count,nullptr),"enumerate GPU count");std::vector<VkPhysicalDevice> devices(count);check(vkEnumeratePhysicalDevices(instance,&count,devices.data()),"enumerate GPUs");
  for(auto candidate:devices){VkPhysicalDeviceIDProperties id{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};VkPhysicalDeviceProperties2 p{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};p.pNext=&id;vkGetPhysicalDeviceProperties2(candidate,&p);
   if(p.properties.deviceType!=VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU&&p.properties.deviceType!=VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU)continue;
   auto key=hex(id.deviceUUID,VK_UUID_SIZE);if(wanted&&*wanted&&key!=wanted)continue;physical=candidate;properties=p.properties;device_uuid=key;driver_uuid=hex(id.driverUUID,VK_UUID_SIZE);name=properties.deviceName;break;}
  if(!physical)throw std::runtime_error("requested physical GPU unavailable; software Vulkan rejected");
  if(segment_bytes>properties.limits.maxStorageBufferRange||properties.limits.maxPerStageDescriptorStorageBuffers<5)throw std::runtime_error("GPU storage limits insufficient");
  vkGetPhysicalDeviceMemoryProperties(physical,&memory_properties);uint32_t ec=0;check(vkEnumerateDeviceExtensionProperties(physical,nullptr,&ec,nullptr),"enumerate extensions");std::vector<VkExtensionProperties> exts(ec);check(vkEnumerateDeviceExtensionProperties(physical,nullptr,&ec,exts.data()),"enumerate extensions");for(auto& e:exts)if(!strcmp(e.extensionName,VK_EXT_MEMORY_BUDGET_EXTENSION_NAME))memory_budget=true;
  for(auto& e:exts)if(!strcmp(e.extensionName,VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME))external_host=true;
  if(external_host){VkPhysicalDeviceExternalMemoryHostPropertiesEXT hp{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT};VkPhysicalDeviceProperties2 pp{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};pp.pNext=&hp;vkGetPhysicalDeviceProperties2(physical,&pp);host_alignment=hp.minImportedHostPointerAlignment;}
  uint32_t qc=0;vkGetPhysicalDeviceQueueFamilyProperties(physical,&qc,nullptr);std::vector<VkQueueFamilyProperties> qp(qc);vkGetPhysicalDeviceQueueFamilyProperties(physical,&qc,qp.data());family=qc;for(uint32_t i=0;i<qc;++i)if(qp[i].queueFlags&VK_QUEUE_COMPUTE_BIT){family=i;break;}if(family==qc)throw std::runtime_error("compute queue unavailable");timestamp_bits=qp[family].timestampValidBits;
  float priority=.25f;VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};qi.queueFamilyIndex=family;qi.queueCount=1;qi.pQueuePriorities=&priority;VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};di.queueCreateInfoCount=1;di.pQueueCreateInfos=&qi;const char* host_ext=VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME;if(external_host){di.enabledExtensionCount=1;di.ppEnabledExtensionNames=&host_ext;}check(vkCreateDevice(physical,&di,nullptr,&device),"create GPU device");vkGetDeviceQueue(device,family,0,&queue);
  VkCommandPoolCreateInfo pi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};pi.queueFamilyIndex=family;pi.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;check(vkCreateCommandPool(device,&pi,nullptr,&pool),"create commands");VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};ca.commandPool=pool;ca.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ca.commandBufferCount=1;check(vkAllocateCommandBuffers(device,&ca,&command),"allocate command buffer");VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};check(vkCreateFence(device,&fi,nullptr,&fence),"create fence");
  io=allocate(max_queries*8,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
  std::array<VkDescriptorSetLayoutBinding,5> bindings{};for(uint32_t i=0;i<5;++i){bindings[i].binding=i;bindings[i].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;bindings[i].descriptorCount=1;bindings[i].stageFlags=VK_SHADER_STAGE_COMPUTE_BIT;}
  VkDescriptorSetLayoutCreateInfo dl{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};dl.bindingCount=bindings.size();dl.pBindings=bindings.data();check(vkCreateDescriptorSetLayout(device,&dl,nullptr,&descriptors),"create descriptor layout");VkPushConstantRange pc{VK_SHADER_STAGE_COMPUTE_BIT,0,16};VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};pl.setLayoutCount=1;pl.pSetLayouts=&descriptors;pl.pushConstantRangeCount=1;pl.pPushConstantRanges=&pc;check(vkCreatePipelineLayout(device,&pl,nullptr,&layout),"create pipeline layout");
  FILE* file=fopen(path,"rb");if(!file)throw std::runtime_error("shader unavailable");if(fseek(file,0,SEEK_END)||ftell(file)<=0){fclose(file);throw std::runtime_error("invalid shader");}size_t length=ftell(file);rewind(file);std::vector<uint32_t> code((length+3)/4);bool complete=length%4==0&&fread(code.data(),1,length,file)==length;fclose(file);if(!complete)throw std::runtime_error("truncated shader");VkShaderModuleCreateInfo si{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};si.codeSize=length;si.pCode=code.data();check(vkCreateShaderModule(device,&si,nullptr,&shader),"create shader");VkComputePipelineCreateInfo cp{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};cp.layout=layout;cp.stage.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;cp.stage.stage=VK_SHADER_STAGE_COMPUTE_BIT;cp.stage.module=shader;cp.stage.pName="main";check(vkCreateComputePipelines(device,VK_NULL_HANDLE,1,&cp,nullptr,&pipeline),"create gather pipeline");
  VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,85};VkDescriptorPoolCreateInfo dp{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};dp.maxSets=17;dp.poolSizeCount=1;dp.pPoolSizes=&ps;check(vkCreateDescriptorPool(device,&dp,nullptr,&descriptor_pool),"create descriptor pool");VkDescriptorSetAllocateInfo da{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};da.descriptorPool=descriptor_pool;da.descriptorSetCount=1;da.pSetLayouts=&descriptors;check(vkAllocateDescriptorSets(device,&da,&set),"allocate descriptors");
 }
 void load(const char* path,const char* expected){
  if(!reference.empty())throw std::runtime_error("immutable object already published");
  int fd=open(path,O_RDONLY|O_NOFOLLOW|O_CLOEXEC);if(fd<0)throw std::runtime_error("reference cannot be opened");struct stat before{},after{};Buffer stage;EVP_MD_CTX* hash=nullptr;
  try{
   if(fstat(fd,&before)||!S_ISREG(before.st_mode)||(before.st_mode&0222)||before.st_size<=0||before.st_size%4||uint64_t(before.st_size)>budget)throw std::runtime_error("reference must be immutable, word-aligned and within owner budget");
   uint64_t bytes=before.st_size;stage=allocate(std::min<uint64_t>(bytes,16ull*1024*1024),VK_BUFFER_USAGE_TRANSFER_SRC_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
   hash=EVP_MD_CTX_new();if(!hash||!EVP_DigestInit_ex(hash,EVP_sha256(),nullptr))throw std::runtime_error("hash initialization failed");
   for(uint64_t base=0;base<bytes;base+=segment_bytes){reference.push_back(allocate(std::min(segment_bytes,bytes-base),VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT));auto& dest=reference.back();
    for(uint64_t offset=0;offset<dest.size;offset+=stage.size){size_t length=std::min<uint64_t>(stage.size,dest.size-offset),at=0;
     while(at<length){ssize_t n=pread(fd,static_cast<char*>(stage.mapped)+at,length-at,base+offset+at);if(n<0&&errno==EINTR)continue;if(n<=0)throw std::runtime_error("reference read failed");at+=n;}
     if(!EVP_DigestUpdate(hash,stage.mapped,length))throw std::runtime_error("reference hash failed");
     begin();VkMemoryBarrier host{VK_STRUCTURE_TYPE_MEMORY_BARRIER};host.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT;host.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT;vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,1,&host,0,nullptr,0,nullptr);VkBufferCopy copy{0,offset,length};vkCmdCopyBuffer(command,stage.buffer,dest.buffer,1,&copy);VkMemoryBarrier ready{VK_STRUCTURE_TYPE_MEMORY_BARRIER};ready.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;ready.dstAccessMask=VK_ACCESS_SHADER_READ_BIT;vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,1,&ready,0,nullptr,0,nullptr);submit();uploaded+=length;
    }
   }
   unsigned char value[EVP_MAX_MD_SIZE];unsigned length=0;if(!EVP_DigestFinal_ex(hash,value,&length)||hex(value,length)!=expected)throw std::runtime_error("reference SHA256 differs; publication rejected");
   if(fstat(fd,&after)||before.st_dev!=after.st_dev||before.st_ino!=after.st_ino||before.st_size!=after.st_size||before.st_mtim.tv_sec!=after.st_mtim.tv_sec||before.st_mtim.tv_nsec!=after.st_mtim.tv_nsec||before.st_ctim.tv_sec!=after.st_ctim.tv_sec||before.st_ctim.tv_nsec!=after.st_ctim.tv_nsec)throw std::runtime_error("reference identity changed during publication");
   object_bytes=bytes;digest=expected;
   std::array<VkDescriptorBufferInfo,5> infos{};std::array<VkWriteDescriptorSet,5> writes{};
   for(uint32_t i=0;i<5;++i){auto& b=i==4?io:reference[i<reference.size()?i:0];infos[i]={b.buffer,0,b.size};writes[i].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[i].dstSet=set;writes[i].dstBinding=i;writes[i].descriptorCount=1;writes[i].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;writes[i].pBufferInfo=&infos[i];}vkUpdateDescriptorSets(device,writes.size(),writes.data(),0,nullptr);
   EVP_MD_CTX_free(hash);destroy(stage);close(fd);
  }catch(...){if(hash)EVP_MD_CTX_free(hash);if(device)vkDeviceWaitIdle(device);destroy(stage);for(auto& b:reference)destroy(b);reference.clear();uploaded=0;close(fd);throw;}
 }
 void gather(uint32_t count){
  if(!object_bytes||!count||count>max_queries)throw std::runtime_error("invalid gather count or unpublished reference");
  auto* input=static_cast<uint32_t*>(io.mapped);for(uint32_t i=0;i<count;++i)if(input[i]>=object_bytes/4)throw std::runtime_error("query index exceeds immutable object");
  begin();VkMemoryBarrier host{VK_STRUCTURE_TYPE_MEMORY_BARRIER};host.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT;host.dstAccessMask=VK_ACCESS_SHADER_READ_BIT;vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,1,&host,0,nullptr,0,nullptr);vkCmdBindPipeline(command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);vkCmdBindDescriptorSets(command,VK_PIPELINE_BIND_POINT_COMPUTE,layout,0,1,&set,0,nullptr);uint32_t limits[4]={count,uint32_t(segment_bytes/4),max_queries,uint32_t(object_bytes/4)};vkCmdPushConstants(command,layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(limits),limits);vkCmdDispatch(command,(count+63)/64,1,1);VkMemoryBarrier done{VK_STRUCTURE_TYPE_MEMORY_BARRIER};done.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;done.dstAccessMask=VK_ACCESS_HOST_READ_BIT;vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&done,0,nullptr,0,nullptr);submit();++queries;
 }
 void configure(uint32_t count){
  if(!object_bytes||!slots.empty()||!count||count>16)throw std::runtime_error("invalid slot configuration");
  for(uint32_t i=0;i<count;++i){
   slots.emplace_back(new Slot);auto& slot=*slots.back();
   slot.io=allocate(max_queries*8,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
   if(staged_io)slot.device_io=allocate(max_queries*8,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
   VkCommandPoolCreateInfo pi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};pi.queueFamilyIndex=family;pi.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;check(vkCreateCommandPool(device,&pi,nullptr,&slot.pool),"create slot pool");
   VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};ca.commandPool=slot.pool;ca.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;ca.commandBufferCount=1;check(vkAllocateCommandBuffers(device,&ca,&slot.command),"allocate slot commands");
   VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};check(vkCreateFence(device,&fi,nullptr,&slot.fence),"create slot fence");
   VkDescriptorSetAllocateInfo da{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};da.descriptorPool=descriptor_pool;da.descriptorSetCount=1;da.pSetLayouts=&descriptors;check(vkAllocateDescriptorSets(device,&da,&slot.set),"allocate slot descriptors");
   std::array<VkDescriptorBufferInfo,5> infos{};std::array<VkWriteDescriptorSet,5> writes{};
   for(uint32_t j=0;j<5;++j){auto& b=j==4?(staged_io?slot.device_io:slot.io):reference[j<reference.size()?j:0];infos[j]={b.buffer,0,b.size};writes[j].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;writes[j].dstSet=slot.set;writes[j].dstBinding=j;writes[j].descriptorCount=1;writes[j].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;writes[j].pBufferInfo=&infos[j];}
   vkUpdateDescriptorSets(device,writes.size(),writes.data(),0,nullptr);
  }
 }
 Slot& slot_at(uint32_t index){if(index>=slots.size())throw std::runtime_error("invalid slot");return *slots[index];}
 void enable_profile(){
  if(!timestamp_bits)throw std::runtime_error("GPU queue timestamps unavailable");
  for(auto& slot:slots){VkQueryPoolCreateInfo ci{VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO};ci.queryType=VK_QUERY_TYPE_TIMESTAMP;ci.queryCount=2;check(vkCreateQueryPool(device,&ci,nullptr,&slot->timestamps),"create profiling timestamps");}
  profiling=true;
 }
 void slot_transport(uint32_t index,bool shared){
  auto& slot=slot_at(index);std::lock_guard<std::mutex> own(slot.busy);
  if(failed)throw std::runtime_error("GPU owner failed; restart required");
  if(!shared&&slot.io.fd<0)return;
  // Always allocate fresh pages for a new shared session: old FDs cannot alias
  // any subsequent client's input/output, even if retained after disconnect.
  Buffer next=shared?allocate_shared():allocate(max_queries*8,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
  auto& binding=staged_io?slot.device_io:next;VkDescriptorBufferInfo bi{binding.buffer,0,binding.size};VkWriteDescriptorSet write{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};write.dstSet=slot.set;write.dstBinding=4;write.descriptorCount=1;write.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;write.pBufferInfo=&bi;vkUpdateDescriptorSets(device,1,&write,0,nullptr);
  destroy(slot.io);slot.io=next;slot.recorded_count=0;
 }
 void slot_gather(uint32_t index,uint32_t count){
  auto start=profiling?Clock::now():Clock::time_point{};
  auto& slot=slot_at(index);std::lock_guard<std::mutex> own(slot.busy);
  if(failed)throw std::runtime_error("GPU owner failed; restart required");
  if(!count||count>max_queries)throw std::runtime_error("invalid gather count");
  auto* input=static_cast<uint32_t*>(slot.io.mapped);for(uint32_t i=0;i<count;++i)if(input[i]>=object_bytes/4)throw std::runtime_error("query index exceeds immutable object");
  // x86 coherent cached imported pages: order accesses through external host
  // aliases around the socket handoff and GPU fence (no payload memcpy).
  std::atomic_thread_fence(std::memory_order_seq_cst);
  auto validated=profiling?Clock::now():Clock::time_point{};
  Gate::Ticket capacity(gate);auto ready=profiling?Clock::now():Clock::time_point{};
  bool submitted=false;
  try{
   if(failed)throw std::runtime_error("GPU owner failed; restart required");
   check(vkResetFences(device,1,&slot.fence),"reset slot fence");
   if(!reuse_commands||slot.recorded_count!=count){
    check(vkResetCommandBuffer(slot.command,0),"reset slot commands");
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};check(vkBeginCommandBuffer(slot.command,&bi),"begin reusable slot commands");
    if(profiling){vkCmdResetQueryPool(slot.command,slot.timestamps,0,2);vkCmdWriteTimestamp(slot.command,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,slot.timestamps,0);}
    VkBufferMemoryBarrier host{VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER};host.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT;host.dstAccessMask=staged_io?VK_ACCESS_TRANSFER_READ_BIT:VK_ACCESS_SHADER_READ_BIT;host.srcQueueFamilyIndex=host.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;host.buffer=slot.io.buffer;host.offset=0;host.size=count*4;
    vkCmdPipelineBarrier(slot.command,VK_PIPELINE_STAGE_HOST_BIT,staged_io?VK_PIPELINE_STAGE_TRANSFER_BIT:VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,0,nullptr,1,&host,0,nullptr);
    if(staged_io){
     VkBufferCopy copy{0,0,count*4ull};vkCmdCopyBuffer(slot.command,slot.io.buffer,slot.device_io.buffer,1,&copy);
     VkBufferMemoryBarrier uploaded=host;uploaded.buffer=slot.device_io.buffer;uploaded.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;uploaded.dstAccessMask=VK_ACCESS_SHADER_READ_BIT;
     vkCmdPipelineBarrier(slot.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,0,nullptr,1,&uploaded,0,nullptr);
    }
    vkCmdBindPipeline(slot.command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);vkCmdBindDescriptorSets(slot.command,VK_PIPELINE_BIND_POINT_COMPUTE,layout,0,1,&slot.set,0,nullptr);
    uint32_t limits[4]={count,uint32_t(segment_bytes/4),max_queries,uint32_t(object_bytes/4)};vkCmdPushConstants(slot.command,layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(limits),limits);vkCmdDispatch(slot.command,(count+63)/64,1,1);
    VkBufferMemoryBarrier done=host;done.offset=max_queries*4ull;done.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;done.dstAccessMask=staged_io?VK_ACCESS_TRANSFER_READ_BIT:VK_ACCESS_HOST_READ_BIT;done.buffer=staged_io?slot.device_io.buffer:slot.io.buffer;
    vkCmdPipelineBarrier(slot.command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,staged_io?VK_PIPELINE_STAGE_TRANSFER_BIT:VK_PIPELINE_STAGE_HOST_BIT,0,0,nullptr,1,&done,0,nullptr);
    if(staged_io){
     VkBufferCopy copy{max_queries*4ull,max_queries*4ull,count*4ull};vkCmdCopyBuffer(slot.command,slot.device_io.buffer,slot.io.buffer,1,&copy);
     done.buffer=slot.io.buffer;done.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;done.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
     vkCmdPipelineBarrier(slot.command,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,nullptr,1,&done,0,nullptr);
    }
    if(profiling)vkCmdWriteTimestamp(slot.command,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,slot.timestamps,1);
    check(vkEndCommandBuffer(slot.command),"end slot commands");slot.recorded_count=count;++command_recordings;
   }else ++command_reuses;
   auto recorded=profiling?Clock::now():Clock::time_point{};
   VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};si.commandBufferCount=1;si.pCommandBuffers=&slot.command;
   {std::lock_guard<std::mutex> lock(queue_lock);if(failed)throw std::runtime_error("GPU owner failed; restart required");check(vkQueueSubmit(queue,1,&si,slot.fence),"submit slot");submitted=true;unsigned n=++pending,peak=peak_pending.load();while(n>peak&&!peak_pending.compare_exchange_weak(peak,n)){} }
   auto submitted_at=profiling?Clock::now():Clock::time_point{};
   // Queue mutex is released before waiting; other slots may submit and complete.
   VkResult status=VK_NOT_READY;
   if(spin_us){auto deadline=Clock::now()+std::chrono::microseconds(spin_us);do{status=vkGetFenceStatus(device,slot.fence);if(status!=VK_NOT_READY)break;
#if defined(__x86_64__)
    __builtin_ia32_pause();
#endif
   }while(Clock::now()<deadline);}
   if(status==VK_NOT_READY)status=vkWaitForFences(device,1,&slot.fence,VK_TRUE,30000000000ull);
   check(status,"wait slot");std::atomic_thread_fence(std::memory_order_seq_cst);--pending;submitted=false;++queries;if(staged_io){upload_copies+=count*4ull;readback_copies+=count*4ull;}
   if(profiling){auto waited=Clock::now();uint64_t ticks[2];check(vkGetQueryPoolResults(device,slot.timestamps,0,2,sizeof(ticks),ticks,sizeof(uint64_t),VK_QUERY_RESULT_64_BIT|VK_QUERY_RESULT_WAIT_BIT),"read GPU timestamps");uint64_t mask=timestamp_bits==64?UINT64_MAX:((1ull<<timestamp_bits)-1);profile.validation_ns+=elapsed(start,validated);profile.capacity_wait_ns+=elapsed(validated,ready);profile.record_ns+=elapsed(ready,recorded);profile.submit_ns+=elapsed(recorded,submitted_at);profile.fence_wait_ns+=elapsed(submitted_at,waited);profile.gpu_span_ns+=uint64_t(((ticks[1]-ticks[0])&mask)*properties.limits.timestampPeriod);profile.total_ns+=elapsed(start,Clock::now());++profile.samples;}
  }catch(...){failed=true;if(submitted)--pending;throw;}
 }
 void serve_shared(uint32_t index,int fd){
  if(slot_at(index).io.fd<0)throw std::runtime_error("native protocol requires shared pages");
  uint8_t bytes[4];
  while(control_transfer(fd,bytes,4,false)){
   uint32_t count=uint32_t(bytes[0])|(uint32_t(bytes[1])<<8)|(uint32_t(bytes[2])<<16)|(uint32_t(bytes[3])<<24),status=0;
   if(!count||count>max_queries)status=1;
   else try{slot_gather(index,count);++native_shared_batches;}catch(const std::exception& e){last_error=e.what();status=failed?3:2;}
   bytes[0]=status;bytes[1]=bytes[2]=bytes[3]=0;control_transfer(fd,bytes,4,true);
   if(status==3)return;
  }
 }
 const char* info(){uint64_t device_io_allocated=0;for(auto& slot:slots)device_io_allocated+=slot->device_io.allocation;uint64_t allocated=0;for(auto& b:reference)allocated+=b.allocation;
  thread_local std::string description;description="{\"device_uuid\":\""+device_uuid+"\",\"driver_uuid\":\""+driver_uuid+"\",\"object_sha256\":\""+digest+"\",\"object_bytes\":"+std::to_string(object_bytes)+",\"reference_allocation_bytes\":"+std::to_string(allocated)+",\"segments\":"+std::to_string(reference.size())+",\"reference_upload_bytes\":"+std::to_string(uploaded)+",\"query_batches\":"+std::to_string(queries.load())+",\"io_bytes\":"+std::to_string(io.size+slots.size()*max_queries*8)+",\"parallelism\":"+std::to_string(slots.size())+",\"peak_pending_submissions\":"+std::to_string(peak_pending.load())+",\"native_shared_batches\":"+std::to_string(native_shared_batches.load())+",\"spin_us\":"+std::to_string(spin_us)+",\"max_queries\":"+std::to_string(max_queries)+",\"profile\":"+profile.json()+",\"command_reuse_enabled\":"+std::string(reuse_commands?"true":"false")+",\"device_io\":\""+std::string(staged_io?"staged":"mapped")+"\",\"device_io_allocation_bytes\":"+std::to_string(device_io_allocated)+",\"command_recordings\":"+std::to_string(command_recordings.load())+",\"command_reuses\":"+std::to_string(command_reuses.load())+",\"query_upload_copy_bytes\":"+std::to_string(upload_copies.load())+",\"result_readback_copy_bytes\":"+std::to_string(readback_copies.load())+"}";return description.c_str();}
};
}
extern "C" {
void* gm_open(const char* shader,const char* device,uint64_t budget,uint64_t segment){Owner* owner=new Owner;try{owner->initialize(shader,device,budget,segment);return owner;}catch(const std::exception& e){last_error=e.what();delete owner;return nullptr;}}
int gm_load(void* p,const char* path,const char* sha){try{static_cast<Owner*>(p)->load(path,sha);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_gather(void* p,uint32_t count){try{static_cast<Owner*>(p)->gather(count);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_slot_transport(void* p,uint32_t slot,int shared){try{static_cast<Owner*>(p)->slot_transport(slot,shared);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_slot_fd(void* p,uint32_t slot){try{return static_cast<Owner*>(p)->slot_at(slot).io.fd;}catch(const std::exception& e){last_error=e.what();return -1;}}
uint64_t gm_slot_mapping_bytes(void* p,uint32_t slot){try{return static_cast<Owner*>(p)->slot_at(slot).io.backing_size;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_optimize(void* p,int reuse,int staged){auto* o=static_cast<Owner*>(p);if(!o->slots.empty()){last_error="optimization must be configured before slots";return 0;}o->reuse_commands=reuse;o->staged_io=staged;return 1;}
int gm_tune(void* p,uint32_t parallelism,uint32_t spin_us){if(!parallelism||parallelism>16||spin_us>100){last_error="invalid parallelism or spin budget";return 0;}auto* o=static_cast<Owner*>(p);o->gate.limit=parallelism;o->spin_us=spin_us;return 1;}
int gm_serve_shared(void* p,uint32_t slot,int fd){try{static_cast<Owner*>(p)->serve_shared(slot,fd);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_profile(void* p){try{static_cast<Owner*>(p)->enable_profile();return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_healthy(void* p){return !static_cast<Owner*>(p)->failed.load();}
int gm_configure(void* p,uint32_t count){try{static_cast<Owner*>(p)->configure(count);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
int gm_slot_gather(void* p,uint32_t slot,uint32_t count){try{static_cast<Owner*>(p)->slot_gather(slot,count);return 1;}catch(const std::exception& e){last_error=e.what();return 0;}}
void* gm_slot_io(void* p,uint32_t slot){try{return static_cast<Owner*>(p)->slot_at(slot).io.mapped;}catch(const std::exception& e){last_error=e.what();return nullptr;}}
void* gm_io(void* p){return static_cast<Owner*>(p)->io.mapped;}
const char* gm_info(void* p){return static_cast<Owner*>(p)->info();}
const char* gm_error(){return last_error.c_str();}
void gm_close(void* p){delete static_cast<Owner*>(p);}
}
