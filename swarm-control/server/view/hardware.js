export function hardwareLines(hardware={}){
 const lines=[];
 const gib=bytes=>(bytes/2**30).toFixed(1)+' GiB';
 if(hardware.storage)lines.push('Disk: '+gib(hardware.storage.free_bytes)+' free / '+gib(hardware.storage.total_bytes));
 for(const gpu of hardware.gpus||[]){
  const parts=[gpu.name];
  if(gpu.memory_total_mib!=null)parts.push((gpu.memory_total_mib/1024).toFixed(1)+' GiB GPU memory');
  if(!hardware.stale&&gpu.utilization_percent!=null)parts.push(gpu.utilization_percent+'% GPU');
  if(!hardware.stale&&gpu.memory_free_mib!=null)parts.push((gpu.memory_free_mib/1024).toFixed(1)+' GiB VRAM free');
  parts.push(gpu.vendor==='NVIDIA'?(hardware.cuda_container_runtime===true?'CUDA container runtime':hardware.cuda_container_runtime===false?'CUDA runtime missing':'CUDA runtime unverified'):gpu.backend||'Compute backend unverified');
  if(gpu.utilization_basis)parts.push(gpu.utilization_basis);
  lines.push(parts.join(' · '));
 }
 if(hardware.gpu_validation?.status==='passed')lines.push('CUDA kernel verified · '+new Date(hardware.gpu_validation.verified_at*1000).toLocaleTimeString());
 if(hardware.stale)lines.push('Hardware sample stale');
 return lines;
}

export async function probeBrowserGPU(gpu=globalThis.navigator?.gpu){
 if(!gpu)return {status:'unavailable',reason:'WebGPU is not exposed in this browser and context'};
 const adapter=await gpu.requestAdapter();
 if(!adapter)return {status:'unavailable',reason:'No WebGPU adapter available'};
 const device=await adapter.requestDevice();
 try{
  const buffer=device.createBuffer({size:4,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_SRC});
  const read=device.createBuffer({size:4,usage:GPUBufferUsage.COPY_DST|GPUBufferUsage.MAP_READ});
  const pipeline=device.createComputePipeline({layout:'auto',compute:{module:device.createShaderModule({code:'@group(0) @binding(0) var<storage,read_write> result:array<u32>; @compute @workgroup_size(1) fn main(){result[0]=42u;}'}),entryPoint:'main'}});
  const bindings=device.createBindGroup({layout:pipeline.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer}}]});
  const encoder=device.createCommandEncoder(),pass=encoder.beginComputePass();
  pass.setPipeline(pipeline);pass.setBindGroup(0,bindings);pass.dispatchWorkgroups(1);pass.end();encoder.copyBufferToBuffer(buffer,0,read,0,4);
  device.queue.submit([encoder.finish()]);await read.mapAsync(GPUMapMode.READ);
  const value=new Uint32Array(read.getMappedRange())[0];read.unmap();buffer.destroy();read.destroy();
  if(value!==42)throw Error('GPU readback mismatch');
  const description=adapter.info?.description||adapter.info?.architecture||'Browser GPU';
  const software=adapter.isFallbackAdapter===true||adapter.info?.isFallbackAdapter===true||/swiftshader|llvmpipe|software/i.test(description);
  return {status:'passed',reason:'WebGPU compute kernel returned 42'+(software?' (software adapter)':''),adapter:description,hardware_accelerated:software?false:null};
 }finally{device.destroy();}
}
