//! Optional requested-byte accounting for Rust's System allocator only.
//! Native sherpa/ORT allocations bypass this wrapper. Never trims or changes
//! allocation sizes; allocator overhead is outside requested-byte counts.
#[cfg(feature = "allocation-diagnostics")]
mod tracked {
    use std::alloc::{GlobalAlloc, Layout, System};
    use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};
    pub struct Allocator;
    pub static BYTES: AtomicUsize = AtomicUsize::new(0);
    pub static BLOCKS: AtomicUsize = AtomicUsize::new(0);
    pub static ALLOCS: AtomicUsize = AtomicUsize::new(0);
    unsafe impl GlobalAlloc for Allocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let ptr = unsafe { System.alloc(layout) };
            if !ptr.is_null() {
                BYTES.fetch_add(layout.size(), Relaxed);
                BLOCKS.fetch_add(1, Relaxed);
                ALLOCS.fetch_add(1, Relaxed);
            }
            ptr
        }
        unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
            let ptr = unsafe { System.alloc_zeroed(layout) };
            if !ptr.is_null() {
                BYTES.fetch_add(layout.size(), Relaxed);
                BLOCKS.fetch_add(1, Relaxed);
                ALLOCS.fetch_add(1, Relaxed);
            }
            ptr
        }
        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            unsafe { System.dealloc(ptr, layout) };
            BYTES.fetch_sub(layout.size(), Relaxed);
            BLOCKS.fetch_sub(1, Relaxed);
        }
        unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
            let next = unsafe { System.realloc(ptr, layout, size) };
            if !next.is_null() {
                if size >= layout.size() {
                    BYTES.fetch_add(size - layout.size(), Relaxed);
                } else {
                    BYTES.fetch_sub(layout.size() - size, Relaxed);
                }
                ALLOCS.fetch_add(1, Relaxed);
            }
            next
        }
    }
}
#[cfg(feature = "allocation-diagnostics")]
#[global_allocator]
static ALLOCATOR: tracked::Allocator = tracked::Allocator;

pub fn snapshot() -> serde_json::Value {
    #[cfg(feature = "allocation-diagnostics")]
    {
        use std::sync::atomic::Ordering::Relaxed;
        let bytes = tracked::BYTES.load(Relaxed);
        let blocks = tracked::BLOCKS.load(Relaxed);
        let allocs = tracked::ALLOCS.load(Relaxed);
        serde_json::json!({"requested_live_bytes":bytes,"live_blocks":blocks,"allocation_calls":allocs,
            "scope":"Rust System allocator requested bytes; excludes native allocations and allocator overhead"})
    }
    #[cfg(not(feature = "allocation-diagnostics"))]
    {
        serde_json::Value::Null
    }
}
