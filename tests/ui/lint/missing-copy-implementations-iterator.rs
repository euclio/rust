//! It is generally considered a footgun to impl `Copy` and `Iterator` on the same type.

//@ check-pass

#![deny(missing_copy_implementations)]

pub struct Struct {
    pub field: i32,
}

impl Iterator for Struct {
    type Item = i32;

    fn next(&mut self) -> Option<Self::Item> {
        None
    }
}

fn main() {}
