use rustc_span::Span;

use pulldown_cmark::{Parser, Options, Event, Tag, LinkType};
use spellbound::Checker;

use crate::clean;
use crate::fold::DocFolder;
use crate::core::DocContext;
use crate::passes::{Pass, span_of_attrs};

pub const SPELLCHECK: Pass = Pass {
    name: "spellcheck",
    run: spellcheck,
    description: "spellcheck",
};

pub fn spellcheck(krate: clean::Crate, cx: &DocContext<'_>) -> clean::Crate {
    Spellchecker { cx, checker: Checker::new(), }.fold_crate(krate)
}

struct Spellchecker<'a, 'tcx> {
    cx: &'a DocContext<'tcx>,
    checker: Checker,
}

impl <'a, 'tcx> Spellchecker<'a, 'tcx> {
    fn check(&mut self, dox: &str, sp: Span) {
        let mut plain_text = String::new();

        let parser = Parser::new_ext(dox, Options::all());

        let mut ignore = false;
        for event in parser {
            match event {
                Event::Start(Tag::CodeBlock(..)) | Event::Start(Tag::Link(LinkType::Autolink, ..)) => ignore = true,
                Event::End(Tag::CodeBlock(..)) | Event::End(Tag::Link(..)) => ignore = false,
                Event::End(Tag::Paragraph) | Event::End(Tag::Heading(..)) | Event::End(Tag::Item) => plain_text.push(' '),
                Event::Text(text) if !ignore => plain_text.push_str(&text),
                Event::SoftBreak | Event::HardBreak if !ignore => plain_text.push(' '),
                _ => (),
            }
        }

        let errors = self.checker.check(&plain_text).collect::<Vec<_>>();

        if !errors.is_empty() {
            let mut diag = self.cx.sess().struct_span_warn(
                sp,
                "spelling errors",
            );

            for err in errors {
                diag.note(&format!("{:?}", err.text()));
            }

            diag.emit();
        }
    }
}

impl <'a, 'tcx> DocFolder for Spellchecker<'a, 'tcx> {
    fn fold_item(&mut self, item: clean::Item) -> Option<clean::Item> {
        if let Some(dox) = &item.attrs.collapsed_doc_value() {
            let sp = span_of_attrs(&item.attrs).unwrap_or(item.source.span());
            self.check(dox, sp);
        }

        self.fold_item_recur(item)
    }
}
