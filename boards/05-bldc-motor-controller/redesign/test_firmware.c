/* Host test: all six commutations use two distinct phases, visit one cycle,
 * and reject the two disconnected/illegal Hall states. */
#include <assert.h>
#include "firmware/commutation.h"
int main(void) {
    unsigned visited=0; unsigned h=1;
    assert(!valid_hall(0) && !valid_hall(7));
    for (unsigned i=0;i<6;i++) {
        assert(valid_hall(h)); assert(!(visited&(1u<<h))); visited|=1u<<h;
        assert(high_phase[h]<3 && low_phase[h]<3);
        assert(high_phase[h]!=low_phase[h]);
        unsigned next=next_hall[h], diff=h^next;
        assert((high_phase[h]==high_phase[next]) != (low_phase[h]==low_phase[next]));
        assert(diff && !(diff&(diff-1))); /* one Hall bit changes */
        h=next;
    }
    assert(h==1 && visited==0x7e);
}
