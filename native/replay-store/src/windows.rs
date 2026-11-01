//! Explicit user-only security at creation, including on files. Do not rely on
//! inherited default DACLs (or Wine's POSIX-mode translation) for captured video.
use std::{
    ffi::c_void,
    fs::File,
    io,
    os::windows::{ffi::OsStrExt, io::FromRawHandle},
    path::Path,
    ptr,
};

type Handle = *mut c_void;
#[repr(C)]
struct Attributes {
    size: u32,
    descriptor: *mut c_void,
    inherit: i32,
}

#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
    fn GetTokenInformation(
        token: Handle,
        class: u32,
        data: *mut c_void,
        bytes: u32,
        needed: *mut u32,
    ) -> i32;
    fn ConvertSidToStringSidW(sid: *mut c_void, text: *mut *mut u16) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        text: *const u16,
        revision: u32,
        descriptor: *mut *mut c_void,
        size: *mut u32,
    ) -> i32;
    fn SetFileSecurityW(path: *const u16, info: u32, descriptor: *mut c_void) -> i32;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn LocalFree(data: *mut c_void) -> *mut c_void;
    fn CreateDirectoryW(path: *const u16, attributes: *const Attributes) -> i32;
    fn CreateFileW(
        path: *const u16,
        access: u32,
        share: u32,
        attributes: *const Attributes,
        disposition: u32,
        flags: u32,
        template: Handle,
    ) -> Handle;
}

struct Local(*mut c_void);
impl Drop for Local {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}

fn descriptor() -> io::Result<Local> {
    unsafe {
        let mut token = ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), 8, &mut token) == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut needed = 0;
        GetTokenInformation(token, 1, ptr::null_mut(), 0, &mut needed);
        if needed == 0 || needed > 65536 {
            CloseHandle(token);
            return Err(io::Error::other("Cannot read replay owner's account SID."));
        }
        // TOKEN_USER starts with SID_AND_ATTRIBUTES; align the buffer for its
        // pointer field rather than casting an unaligned Vec<u8>.
        let mut data = vec![0usize; (needed as usize).div_ceil(std::mem::size_of::<usize>())];
        let result = GetTokenInformation(token, 1, data.as_mut_ptr().cast(), needed, &mut needed);
        let token_error = io::Error::last_os_error();
        CloseHandle(token);
        if result == 0 {
            return Err(token_error);
        }
        let sid = data[0] as *mut c_void;
        let mut text = ptr::null_mut();
        if ConvertSidToStringSidW(sid, &mut text) == 0 {
            return Err(io::Error::last_os_error());
        }
        let text_owner = Local(text.cast());
        let mut length = 0;
        while *text.add(length) != 0 {
            length += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, length));
        drop(text_owner);
        let sddl: Vec<u16> = format!("D:P(A;OICI;FA;;;{sid})")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        let mut descriptor = ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            ptr::null_mut(),
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Local(descriptor))
    }
}

fn name(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain(Some(0)).collect()
}
fn attributes(descriptor: &Local) -> Attributes {
    Attributes {
        size: std::mem::size_of::<Attributes>() as u32,
        descriptor: descriptor.0,
        inherit: 0,
    }
}

pub fn directory(path: &Path) -> io::Result<()> {
    let descriptor = descriptor()?;
    let attributes = attributes(&descriptor);
    if unsafe { CreateDirectoryW(name(path).as_ptr(), &attributes) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { SetFileSecurityW(name(path).as_ptr(), 0x80000004, descriptor.0) } == 0 {
        let error = io::Error::last_os_error();
        let _ = std::fs::remove_dir(path);
        return Err(error);
    }
    Ok(())
}

pub fn file(path: &Path) -> io::Result<File> {
    let descriptor = descriptor()?;
    let attributes = attributes(&descriptor);
    let handle = unsafe {
        CreateFileW(
            name(path).as_ptr(),
            0xc0000000,
            7,
            &attributes,
            1,
            0x80,
            ptr::null_mut(),
        )
    };
    if handle as isize == -1 {
        return Err(io::Error::last_os_error());
    }
    // Wine may derive extra ACEs from the Unix creation mode even when supplied
    // an explicit descriptor. Reapply protection before any video bytes arrive.
    if unsafe { SetFileSecurityW(name(path).as_ptr(), 0x80000004, descriptor.0) } == 0 {
        let error = io::Error::last_os_error();
        unsafe {
            CloseHandle(handle);
        }
        let _ = std::fs::remove_file(path);
        return Err(error);
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}
