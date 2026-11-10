"""Conservative DOCX equivalence: retain every part except save metadata.

Never use the semantic digest as a physical-file checksum. Unknown, signed,
oversized or malformed packages fail closed and remain separate.
"""
import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
IGNORED = {'docProps/core.xml', 'docProps/app.xml'}
REL_TYPES = {
    'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties',
}


def fingerprint(path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if ('word/document.xml' not in names or '[Content_Types].xml' not in names
                    or len(names) != len(set(names)) or len(names) > 2000
                    or any(n.startswith('_xmlsignatures/') for n in names)
                    or sum(e.file_size for e in entries) > 128 * 1024 * 1024):
                return None
            digest = hashlib.sha256()
            for entry in sorted(entries, key=lambda e: e.filename):
                name = entry.filename
                if entry.is_dir() or name in IGNORED:
                    continue
                if entry.file_size > 64 * 1024 * 1024:
                    return None
                body = archive.read(entry)
                if name.endswith(('.xml', '.rels')):
                    if b'<!DOCTYPE' in body or b'<!ENTITY' in body:
                        return None
                    root = ET.fromstring(body)
                    for parent in root.iter():
                        # Ignore only Word's edit-session IDs, never revision content.
                        for key in list(parent.attrib):
                            if key.startswith('{' + W + '}rsid'):
                                del parent.attrib[key]
                        for child in list(parent):
                            if (name == 'word/settings.xml' and child.tag == '{' + W + '}rsids'
                                    or name == '[Content_Types].xml' and child.get('PartName', '').lstrip('/') in IGNORED
                                    or name == '_rels/.rels' and child.get('Type') in REL_TYPES):
                                parent.remove(child)
                    body = ET.canonicalize(ET.tostring(root, encoding='unicode')).encode('utf-8')
                encoded = name.encode('utf-8')
                digest.update(len(encoded).to_bytes(8, 'big') + encoded)
                digest.update(len(body).to_bytes(8, 'big') + body)
            return digest.hexdigest()
    except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, ET.ParseError):
        return None


def merge(files, reviews=None, loader=None):
    """Keep a stable representative and all original observations/checksums."""
    reviews = reviews or {}
    groups, out = {}, []
    for f in files:
        key = None
        if f.ext.lower() == '.docx' and (f.cache_path or loader) and f.content_hash:
            try:
                # A changed historical file must not authorize removal of another file.
                if f.cache_path:
                    from .archive import _sha256
                    verified = _sha256(f.cache_path) == f.content_hash
                    source = f.cache_path
                else:
                    body = loader(f)
                    verified = body is not None and hashlib.sha256(body).hexdigest() == f.content_hash
                    source = io.BytesIO(body) if body is not None else None
            except OSError:
                verified = False
            if verified:
                key = fingerprint(source)
        # Explicit business decisions and document revision identities remain distinct.
        policy = (f.type_name, f.folder, f.secret,
                  repr(sorted(reviews.get(f.ident, {}).items())),
                  tuple(sorted({(o.document_id, o.revision) for o in f.obs if o.document_id})))
        group = (key, policy)
        if key is None or group not in groups:
            out.append(f)
            if key is not None:
                groups[group] = f
            continue
        target = groups[group]
        target.equivalent_hashes = sorted(set(target.equivalent_hashes + f.equivalent_hashes
                                            + [f.content_hash]) - {target.content_hash})
        target.obs.extend(o for o in f.obs if o not in target.obs)
    return out
