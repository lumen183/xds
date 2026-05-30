#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/mm.h>

#include "p2p_mem_query.h"

/*
 * Stub implementations of devmm memory query helpers.
 * Replace or override these when linking against the real devmm driver.
 */
int devmm_get_mem_pa_list(struct devmm_svm_process_id *process_id, u64 addr, u64 size,
			  u64 *pa_list, u32 pa_num)
{
	return -ENOSYS;
}
EXPORT_SYMBOL(devmm_get_mem_pa_list);

void devmm_put_mem_pa_list(struct devmm_svm_process_id *process_id, u64 addr, u64 size,
			   u64 *pa_list, u32 pa_num)
{
}
EXPORT_SYMBOL(devmm_put_mem_pa_list);

u32 devmm_get_mem_page_size(struct devmm_svm_process_id *process_id, u64 addr, u64 size)
{
	return PAGE_SIZE;
}
EXPORT_SYMBOL(devmm_get_mem_page_size);

static int __init stub_init(void)
{
	return 0;
}

static void __exit stub_exit(void)
{
}

module_init(stub_init);
module_exit(stub_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Stub devmm symbols for p2p_dev");
