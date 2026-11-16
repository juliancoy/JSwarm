// Native CPU baseline: identical bounds validation and uint32 indexed gather.
#include <cstdint>
extern "C" int cpu_gather(const uint32_t* reference,uint64_t words,
                          const uint32_t* input,uint32_t* output,uint32_t count){
 if(!count||count>65536)return 0;
 for(uint32_t i=0;i<count;++i)if(input[i]>=words)return 0;
 for(uint32_t i=0;i<count;++i)output[i]=reference[input[i]];
 return 1;
}
extern "C" uint32_t cpu_indices(uint32_t* output,uint32_t count,uint32_t words,uint32_t seed){
 uint32_t state=seed?seed:1;const bool power_two=words&&!(words&(words-1));
 for(uint32_t i=0;i<count;++i){state^=state<<13;state^=state>>17;state^=state<<5;output[i]=power_two?state&(words-1):state%words;}
 return state;
}
