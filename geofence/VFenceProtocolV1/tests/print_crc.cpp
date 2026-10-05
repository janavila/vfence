#include <iostream>
#include "../vfence_protocol.h"
using namespace vfence;
int main(){
 CoordinateE7 p[4]={{-313131703,-540868848},{-313131703,-540867535},{-313133945,-540867535},{-313133945,-540868848}};
 std::cout<<std::hex<<computeFenceCrc32(2,p,4)<<"\n";
}
