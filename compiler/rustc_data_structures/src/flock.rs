//! Simple file-locking apis for each OS.
//!
//! This is not meant to be in the standard library, it does nothing with
//! green/native threading. This is just a bare-bones enough solution for
//! librustdoc, it is not production quality at all.

#![allow(non_camel_case_types)]
#![allow(nonstandard_style)]

use std::fs::{File, OpenOptions};
use std::io;
use std::path::Path;

cfg_if! {
    // We use `flock` rather than `fcntl` on Linux, because WSL1 does not support
    // `fcntl`-style advisory locks properly (rust-lang/rust#72157).
    //
    // For other Unix targets we still use `fcntl` because it's more portable than
    // `flock`.
    if #[cfg(target_os = "linux")] {
        use std::os::unix::prelude::*;

        #[derive(Debug)]
        pub struct Lock {
            _file: File,
        }

        impl Lock {
            pub fn new(p: &Path,
                       wait: bool,
                       create: bool,
                       exclusive: bool)
                       -> io::Result<Lock> {
                let file = OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(create)
                    .mode(libc::S_IRWXU as u32)
                    .open(p)?;

                let mut operation = if exclusive {
                    libc::LOCK_EX
                } else {
                    libc::LOCK_SH
                };
                if !wait {
                    operation |= libc::LOCK_NB
                }

                let ret = unsafe { libc::flock(file.as_raw_fd(), operation) };
                if ret == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(Lock { _file: file })
                }
            }

            pub fn error_unsupported(err: &io::Error) -> bool {
                matches!(err.raw_os_error(), Some(libc::ENOTSUP) | Some(libc::ENOSYS))
            }
        }

        // Note that we don't need a Drop impl to execute `flock(fd, LOCK_UN)`. Lock acquired by
        // `flock` is associated with the file descriptor and closing the file release it
        // automatically.
    } else if #[cfg(unix)] {
        use std::mem;
        use std::os::unix::prelude::*;

        #[derive(Debug)]
        pub struct Lock {
            file: File,
        }

        impl Lock {
            pub fn new(p: &Path,
                       wait: bool,
                       create: bool,
                       exclusive: bool)
                       -> io::Result<Lock> {
                let file = OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(create)
                    .mode(libc::S_IRWXU as u32)
                    .open(p)?;

                let lock_type = if exclusive {
                    libc::F_WRLCK
                } else {
                    libc::F_RDLCK
                };

                let mut flock: libc::flock = unsafe { mem::zeroed() };
                flock.l_type = lock_type as libc::c_short;
                flock.l_whence = libc::SEEK_SET as libc::c_short;
                flock.l_start = 0;
                flock.l_len = 0;

                let cmd = if wait { libc::F_SETLKW } else { libc::F_SETLK };
                let ret = unsafe {
                    libc::fcntl(file.as_raw_fd(), cmd, &flock)
                };
                if ret == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(Lock { file })
                }
            }

            pub fn error_unsupported(err: &io::Error) -> bool {
                matches!(err.raw_os_error(), Some(libc::ENOTSUP) | Some(libc::ENOSYS))
            }
        }

        impl Drop for Lock {
            fn drop(&mut self) {
                let mut flock: libc::flock = unsafe { mem::zeroed() };
                flock.l_type = libc::F_UNLCK as libc::c_short;
                flock.l_whence = libc::SEEK_SET as libc::c_short;
                flock.l_start = 0;
                flock.l_len = 0;

                unsafe {
                    libc::fcntl(self.file.as_raw_fd(), libc::F_SETLK, &flock);
                }
            }
        }
    } else if #[cfg(windows)] {
        use std::mem::MaybeUninit;
        use std::os::windows::prelude::*;

        use windows::Win32::{
            Foundation::{ERROR_INVALID_FUNCTION, HANDLE},
            Storage::FileSystem::{
                LOCK_FILE_FLAGS,
                LockFileEx, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
                LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
            },
            System::IO::OVERLAPPED,
        };

        #[derive(Debug)]
        pub struct Lock {
            _file: File,
        }

        impl Lock {
            pub fn new(p: &Path,
                       wait: bool,
                       create: bool,
                       exclusive: bool)
                       -> io::Result<Lock> {
                assert!(p.parent().unwrap().exists(),
                    "Parent directory of lock-file must exist: {}",
                    p.display());

                let share_mode = FILE_SHARE_DELETE | FILE_SHARE_READ | FILE_SHARE_WRITE;

                let mut open_options = OpenOptions::new();
                open_options.read(true)
                            .share_mode(share_mode.0);

                if create {
                    open_options.create(true)
                                .write(true);
                }

                debug!("attempting to open lock file `{}`", p.display());
                let file = match open_options.open(p) {
                    Ok(file) => {
                        debug!("lock file opened successfully");
                        file
                    }
                    Err(err) => {
                        debug!("error opening lock file: {}", err);
                        return Err(err)
                    }
                };

                let mut dwFlags = LOCK_FILE_FLAGS::default();
                if !wait {
                    dwFlags |= LOCKFILE_FAIL_IMMEDIATELY;
                }

                if exclusive {
                    dwFlags |= LOCKFILE_EXCLUSIVE_LOCK;
                }

                let mut overlapped = MaybeUninit::<OVERLAPPED>::zeroed();

                debug!("attempting to acquire lock on lock file `{}`", p.display());
                unsafe {
                    LockFileEx(
                        HANDLE(file.as_raw_handle() as _),
                        dwFlags,
                        0,
                        u32::MAX,
                        u32::MAX,
                        overlapped.as_mut_ptr(),
                    )
                }
                .ok()
                .map_err(|e| {
                    debug!("failed acquiring file lock: {}", e);
                    io::Error::from_raw_os_error(e.code().0)
                })?;

                debug!("successfully acquired lock");
                Ok(Lock { _file: file })
            }

            pub fn error_unsupported(err: &io::Error) -> bool {
                err.raw_os_error() == Some(ERROR_INVALID_FUNCTION.0 as i32)
            }
        }

        // Note that we don't need a Drop impl on the Windows: The file is unlocked
        // automatically when it's closed.
    } else {
        #[derive(Debug)]
        pub struct Lock(());

        impl Lock {
            pub fn new(_p: &Path, _wait: bool, _create: bool, _exclusive: bool)
                -> io::Result<Lock>
            {
                let msg = "file locks not supported on this platform";
                Err(io::Error::new(io::ErrorKind::Other, msg))
            }

            pub fn error_unsupported(_err: &io::Error) -> bool {
                true
            }
        }
    }
}
