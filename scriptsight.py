"""
GUI Image Explorer: Filter and preview images based on JSON annotations.
Requirements: pysimplegui (free on PyPI), Pillow
"""
import os
import io
import sys
import json
import shutil
from pathlib import Path
import uuid
from datetime import datetime
import threading
import queue

# GUI import prefers free PyPI package
try:
    import pysimplegui as sg
except ImportError:
    import PySimpleGUI as sg

from PIL import Image, ImageDraw, ImageFont

centers = {
    'black': (10, 10, 10),
    'grey': (150, 150, 150),
    'blue': (60, 60, 190),
    'red': (200, 20, 0),
    'white': (255, 255, 255),
    # 'orange': (255, 165, 0),
    # 'yellow': (255, 255, 0),
    'green': (0, 255, 0),
    # 'cyan': (0, 255, 255),
    # 'light_blue': (173, 216, 230),
    # 'navy': (0, 0, 128),
    # 'purple': (128, 0, 128),
    # 'brown': (150, 75, 0),
}
# define your “super‐groups”, any others you want to collapse, or leave unlisted to pass through
group_map = {
    'blue': ['blue'],  # , 'light_blue', 'cyan', 'purple'],
    'red': ['red'],  # , 'orange'],
    'black': ['black'],  # , 'brown'],
}
# invert for quick lookup
parent = {}
for big, childs in group_map.items():
    for c in childs:
        parent[c] = big


# ---- Configuration ----
def app_folder():
    # if frozen by PyInstaller, sys.executable is the .exe
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # running as a normal script
    return Path(__file__).parent


SCRIPT_DIR = app_folder()
# make sure these live next to the EXE, not in a temp dir
(SCRIPT_DIR / ".thumb_cache").mkdir(exist_ok=True)
(SCRIPT_DIR / "output").mkdir(exist_ok=True)

CONFIG_PATH = SCRIPT_DIR / 'config.json'
DEFAULT_CONFIG = {
    'json_folder': '',
    'img_folder': '',
    'out_folder': '',
    'thumb_size': 128,
    'min_score': 0.0,
    'min_area': 0.0,
    'cache_folder': str(SCRIPT_DIR / '.thumb_cache'),
    'cache_enabled': False,
    'show_tool_labels': True,
}


# ---- Annotation Cache ----
# mapping from JSON file path to {"mtime": float, "data": dict}
_ANNOTATION_CACHE = {}


def _load_json_cached(path):
    """Load and cache JSON data, reusing parsed content when unchanged."""
    path = str(path)
    mtime = os.path.getmtime(path)
    entry = _ANNOTATION_CACHE.get(path)
    if entry and entry["mtime"] == mtime:
        return entry["data"]

    data = json.loads(Path(path).read_text())
    _ANNOTATION_CACHE[path] = {"mtime": mtime, "data": data}
    return data


def clear_annotation_cache():
    """Clear cached annotation data.

    Should be called when the user switches JSON directories to avoid
    cross-directory cache contamination.
    """
    _ANNOTATION_CACHE.clear()


# ---- Popup ----
def show_error(msg):
    layout = [[sg.Text(msg)], [sg.Button('OK')]]
    win = sg.Window('Error', layout, modal=True, finalize=True)
    while True:
        ev, _ = win.read()
        if ev in (sg.WIN_CLOSED, 'OK'):
            break
    win.close()


# ---- Config I/O ----
def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            cfg = {**DEFAULT_CONFIG, **cfg}
            cfg['show_tool_labels'] = bool(cfg.get('show_tool_labels', True))
            return cfg
        except:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    try:
        data = {**DEFAULT_CONFIG, **cfg}
        CONFIG_PATH.write_text(json.dumps(data, indent=4))
    except:
        pass


# ---- Helpers ----
def parse_color(code):
    try:
        return tuple(map(int, code.split('-')))
    except:
        return (255, 255, 255)


def rgb_to_label(code):
    r, g, b = map(int, code.split('-'))
    # quick-exit rules (in order)
    if max(r, g, b) < 60:
        return 'black'
    if min(r, g, b) > 220:
        return 'white'
    if max(r, g, b) - min(r, g, b) < 20:
        return 'grey'

    # pure primaries & secondary
    if r > g + 30 and r > b + 30:
        return 'red'
    if g > r + 50 and g > b + 50:
        return 'green'
    if b > r + 30 and b > g + 30:
        return 'blue'

    # distance fallback
    dists = {
        lbl: (r - ctr[0]) ** 2 + (g - ctr[1]) ** 2 + (b - ctr[2]) ** 2
        for lbl, ctr in centers.items()
    }
    nearest = min(dists, key=dists.get)

    # fold into super‐group if defined
    return parent.get(nearest, nearest)


def find_image_file(img_dir, name):
    stem, _ = os.path.splitext(name)
    for ext in ('.jpg', '.png'):
        p = Path(img_dir) / f"{stem}{ext}"
        if p.exists():
            return str(p)
    return None


# ---- Data Scanning ----
def gather_properties(json_folder):
    tools, orients, colors = set(), set(), set()
    for jf in Path(json_folder).glob('*.json'):
        data = _load_json_cached(jf)
        for ann in data.get('annotations', []):
            wt = ann.get('writing_tool', '').lower()
            ori = ann.get('orientation', '').lower()
            tools.add(wt) if wt else None
            orients.add(ori) if ori else None
            colors.add(rgb_to_label(ann.get('color_code', '0-0-0')))
    return sorted(tools), sorted(orients), sorted(colors)


# ---- Filtering ----
def filter_and_collect(json_folder, img_root, sel_tools, sel_orients, sel_colors,
                       no_words, min_score=0.0, area_ratio=0.0):
    results = []
    for jf in Path(json_folder).glob('*.json'):
        data = _load_json_cached(jf)
        ann_map = {}
        for ann in data.get('annotations', []):
            ann_map.setdefault(ann['image_id'], []).append(ann)
        folder = Path(img_root) / jf.stem

        for img in data.get('images', []):
            anns = ann_map.get(img['id'], [])

            # 1) drop page-crop anns
            anns = [a for a in anns if 'page_position' not in a]

            # 2) confidence threshold
            anns = [a for a in anns if a.get('score', 0.0) >= min_score]

            # 3) no_words shortcut
            if no_words:
                if anns:
                    continue  # we only want images with zero anns
            else:
                if not anns:
                    continue  # we need at least one annotation

            # 4) compute max area & 5) enforce min_word_size
            local_max = max((float(a.get('area', 0.0)) for a in anns), default=0.0)
            anns = [a for a in anns
                    if float(a.get('area', 0.0)) >= area_ratio * local_max]

            # 6) re-apply no_words / must-have-anns check
            if no_words:
                if anns:
                    continue
            else:
                if not anns:
                    continue

            # 7) apply the tool/orient/color filters once
            if sel_tools:
                anns = [a for a in anns
                        if a.get('writing_tool', '').lower() in sel_tools]
                if not anns:
                    continue

            if sel_orients:
                anns = [a for a in anns
                        if a.get('orientation', '').lower() in sel_orients]
                if not anns:
                    continue

            if sel_colors:
                anns = [a for a in anns
                        if rgb_to_label(a.get('color_code', '0-0-0')) in sel_colors]
                if not anns:
                    continue

            # finally, load the file and record it
            full = find_image_file(folder, Path(img['file_name']).name)
            if full:
                results.append((full, anns))

    return results


# ---- Overlay Drawing & Save ----
def draw_overlay_and_save(src, dst, anns, cfg):
    """Draw annotations on an image and save the result.

    Parameters
    ----------
    src : str or Path
        Source image path.
    dst : str or Path
        Destination path for the saved overlay image.
    anns : list
        Annotations to draw.
    cfg : dict
        Configuration dict controlling overlay options.
    """
    with Image.open(src) as img:
        img = img.convert('RGB')
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        for ann in anns:
            color = parse_color(ann.get('color_code', '255-255-0'))
            tool = ann.get('writing_tool', '').upper()
            for seg in ann.get('segmentation', []):
                pts = [(int(seg[i]), int(seg[i + 1])) for i in range(0, len(seg), 2)]
                draw.line(pts + [pts[0]], width=5, fill=color)
                if tool and cfg.get('show_tool_labels', True):
                    x, y = pts[0]
                    draw.text((x, max(y - 10, 0)), tool, font=font, fill=color)
        img.save(dst)


# ---- Thumbnail Generation ----
def make_thumbnail(full, anns, cfg, overlay):
    sz = cfg['thumb_size']
    cache = Path(cfg['cache_folder']) / str(sz)
    cache.mkdir(parents=True, exist_ok=True)

    # incorporate thresholds to avoid stale overlays
    ms = int(cfg.get('min_score', 0.0) * 100)

    # compute this image’s page bbox area (fallback to full image if no anns)
    with Image.open(full) as img:
        img = img.convert('RGB')
        W, H = img.size

        # find the first annotation that has page_position
        page_ann = next((a for a in anns if 'page_position' in a), None)
        if page_ann:
            xc, yc, rw, rh = page_ann['page_position']
        else:
            # fallback to full-image as “page”
            xc, yc, rw, rh = 0.5, 0.5, 1.0, 1.0

        crop_w, crop_h = rw * W, rh * H
        page_area = crop_w * crop_h

        ma = int(cfg.get('min_area', 0.0) * page_area)
        flag = 'ov' if overlay else 'no'

        stem = Path(full).stem
        thumb = cache / f"{stem}_{flag}_s{ms:02d}_a{ma}.png"

        if cfg['cache_enabled'] and thumb.exists():
            return str(thumb)

        if overlay and anns:
            draw, imgfont = ImageDraw.Draw(img), ImageFont.load_default()
            for ann in anns:
                col = parse_color(ann.get('color_code', '255-255-0'))
                tool = ann.get('writing_tool', '').upper()
                for seg in ann.get('segmentation', []):
                    pts = [(int(seg[i]), int(seg[i + 1])) for i in range(0, len(seg), 2)]
                    draw.line(pts + [pts[0]], width=5, fill=col)
                    if tool and cfg.get('show_tool_labels', True):
                        x, y = pts[0]
                        draw.text((x, max(y - 10, 0)), tool, font=imgfont, fill=col)
        img.thumbnail((sz, sz))
        img.save(thumb)
        return str(thumb)


# ---- Background worker ----
def build_thumbnails(q, vals, cfg):
    """Worker thread to collect files and make thumbnails.

    Progress and completion are reported back via ``q``.
    """
    json_dir = Path(vals['-JSON-'])
    img_dir = Path(vals['-IMG-'])
    if not json_dir.exists():
        show_error(f"JSON folder not found: {json_dir}")
        q.put(('DONE', []))
        return
    if not img_dir.exists():
        show_error(f"Images folder not found: {img_dir}")
        q.put(('DONE', []))
        return

    res = filter_and_collect(
        json_dir, img_dir,
        vals['-TOOLS-'], vals['-ORIENTS-'], vals['-COLORS-'], vals['-NO_WORDS-'],
        float(vals['-MIN_SCORE-']), float(vals['-MIN_AREA-'])
    )
    total = len(res)
    q.put(('TOTAL', total))
    thumbs_local = []
    for i, (full, anns) in enumerate(res):
        thumb = make_thumbnail(full, anns, cfg, vals['-OVERLAY-'])
        thumbs_local.append((thumb, full, anns))
        q.put(('PROGRESS', i + 1, total))
    q.put(('DONE', thumbs_local))


# ---- Main GUI ----
def main():
    cfg = load_config()
    current_json_folder = cfg.get('json_folder', '')

    layout = [
        [sg.Text(
            'Select Paths',
            font=('Any', 13, 'bold'),  # make it stand out
            pad=((0, 0), (3, 3)),  # add some vertical space
            justification='left',
            expand_x=False  # span the full window width
        )],
        [sg.Text('JSON Folder:'), sg.Input(cfg['json_folder'], key='-JSON-', enable_events=True),
         sg.FolderBrowse(target='-JSON-')],
        [sg.Text('Images Root:'), sg.Input(cfg['img_folder'], key='-IMG-', enable_events=True),
         sg.FolderBrowse(target='-IMG-')],
        [sg.Text('Output Folder:'), sg.Input(cfg['out_folder'], key='-OUT-'), sg.FolderBrowse(target='-OUT-')],

        [sg.HorizontalSeparator()],

        [sg.Text(
            'Select Properties',
            font=('Any', 13, 'bold'),  # make it stand out
            pad=((0, 0), (3, 3)),  # add some vertical space
            justification='left',
            expand_x=False  # span the full window width
        )],
        [sg.Text('Writing Implement'), sg.Listbox(values=[], select_mode='multiple', size=(20, 3), key='-TOOLS-'),
         sg.Text('', size=(10, 2)), sg.Checkbox('No-text', key='-NO_WORDS-'),
         sg.Checkbox('Overlay predictions', key='-OVERLAY-')],
        [sg.Text('Text Orientation'), sg.Text('', size=(0, 2)),
         sg.Listbox(values=[], select_mode='multiple', size=(20, 3), key='-ORIENTS-'), sg.Text('', size=(10, 0)),
         sg.Text('Min. confidence'),
         sg.Slider((0.0, 1.0), cfg['min_score'], resolution=0.05, orientation='h', size=(20, 15), key='-MIN_SCORE-'),
         sg.Button('?', key='-HELP_SCORE-', tooltip='What does Min score do?'), ],
        [sg.Text('Text Colour'), sg.Text('', size=(3, 0)),
         sg.Listbox(values=[], select_mode='multiple', size=(20, 5), key='-COLORS-'), sg.Text('', size=(11, 0)),
         sg.Text('Min. word size'),
         sg.Slider((0.0, 1.0), cfg['min_area'], resolution=0.05, orientation='h', size=(20, 15), key='-MIN_AREA-'),
         sg.Button('?', key='-HELP_AREA-', tooltip='What does Min area do?'), ],

        [sg.HorizontalSeparator()],

        [sg.Text(
            'Change Settings',
            font=('Any', 13, 'bold'),  # make it stand out
            pad=((0, 0), (3, 3)),  # add some vertical space
            justification='left',
            expand_x=False  # span the full window width
        )],
        [sg.Checkbox('Enable cache', key='-CACHE-', default=cfg['cache_enabled']), sg.Text('Thumb size'),
         sg.Slider((64, 256), cfg['thumb_size'], orientation='h', size=(20, 15), key='-SLIDER-'),
         sg.Button('Filter & Show'),
         sg.Button('Save results'),

         # spacer to push the next three buttons to the right
         sg.Text('', size=(6, 1)),

         # app-level controls
         sg.Button('Exit'),
         sg.Button('Help'),
         sg.Button('About')
         ],

        [sg.HorizontalSeparator()],
        [sg.Column(
            [[]],
            scrollable=True,
            vertical_scroll_only=True,
            size=(800, 400),
            key='-THUMB_COL-',
            pad=(0, 0),  # ← no outer padding
            element_justification='left'  # ← force children to the left
        )],

        [sg.ProgressBar(1, orientation='h', size=(40, 10), key='-PROG-'), sg.Text('0/0', key='-PCT-')]
    ]

    window = sg.Window('ScriptSight', layout, resizable=False, finalize=True)

    # initial populate of filters
    if cfg['json_folder'] and cfg['img_folder'] and Path(cfg['json_folder']).exists():
        t, o, c = gather_properties(cfg['json_folder'])
        window['-TOOLS-'].update(values=t)
        window['-ORIENTS-'].update(values=o)
        # window['-COLORS-'].update(values=c)

        # ignore the JSON’s colours and use your full master list
        final_colors = list(dict.fromkeys(parent.get(n, n) for n in centers))
        window['-COLORS-'].update(values=final_colors)

    thumbs = []
    key_to_thumb = {}
    worker_q = None
    worker_thread = None
    total = 0

    while True:
        event, vals = window.read(timeout=100)

        # handle background worker updates
        if worker_q is not None:
            try:
                while True:
                    msg = worker_q.get_nowait()
                    kind = msg[0]
                    if kind == 'TOTAL':
                        total = msg[1]
                        window['-PROG-'].update(current_count=0, max=total)
                        window['-PCT-'].update(f"0/{total}")
                    elif kind == 'PROGRESS':
                        current, total = msg[1], msg[2]
                        window['-PROG-'].update(current_count=current)
                        window['-PCT-'].update(f"{current}/{total}")
                    elif kind == 'DONE':
                        thumbs = msg[1]
                        key_to_thumb = {}

                        container = window['-THUMB_COL-'].Widget
                        canvas = next(w for w in container.winfo_children() if w.winfo_class() == 'Canvas')
                        canvas.update_idletasks()
                        canvas_width = canvas.winfo_width()

                        pad = 2
                        thumb_size = cfg['thumb_size']
                        cols = max(1, (canvas_width + pad) // (thumb_size + pad * 2))

                        rows = []
                        for idx, (thumb, full, anns) in enumerate(thumbs):
                            if idx % cols == 0:
                                rows.append([])
                            unique_key = f"IMG_{idx}_{uuid.uuid4().hex}"
                            key_to_thumb[unique_key] = (full, anns)
                            rows[-1].append(
                                sg.Image(
                                    filename=thumb,
                                    key=unique_key,
                                    enable_events=True,
                                    pad=(2, 2)
                                )
                            )

                        thumb_col = window['-THUMB_COL-']
                        container = thumb_col.Widget
                        canvas = next(w for w in container.winfo_children() if w.winfo_class() == 'Canvas')
                        frames = [w for w in canvas.winfo_children() if w.winfo_class() == 'Frame']
                        if frames:
                            first = frames[0]
                            for child in first.winfo_children():
                                child.destroy()
                            for extra in frames[1:]:
                                extra.destroy()

                        window.extend_layout(thumb_col, rows)
                        window.refresh()
                        canvas.configure(scrollregion=canvas.bbox("all"))

                        worker_q = None
                        worker_thread = None
            except queue.Empty:
                pass

        if event == sg.TIMEOUT_KEY:
            continue

        if event in (sg.WIN_CLOSED, 'Exit'):
            cfg.update({
                'json_folder': vals['-JSON-'],
                'img_folder': vals['-IMG-'],
                'out_folder': vals['-OUT-']
            })
            save_config(cfg)
            break

        if event in ('-JSON-', '-IMG-') and vals['-JSON-'] and vals['-IMG-']:
            if vals['-JSON-'] != current_json_folder:
                clear_annotation_cache()
                current_json_folder = vals['-JSON-']
            t, o, c = gather_properties(vals['-JSON-'])
            window['-TOOLS-'].update(values=t)
            window['-ORIENTS-'].update(values=o)
            # window['-COLORS-'].update(values=c)

            # ignore the JSON’s colours and use your full master list
            final_colors = list(dict.fromkeys(parent.get(n, n) for n in centers))
            window['-COLORS-'].update(values=final_colors)

        if event == 'Filter & Show':
            if vals['-JSON-'] != current_json_folder:
                clear_annotation_cache()
                current_json_folder = vals['-JSON-']
            # remember current selections
            sel_tools = vals['-TOOLS-']
            sel_orients = vals['-ORIENTS-']
            sel_colors = vals['-COLORS-']

            # reset mapping
            key_to_thumb = {}
            # refresh filters & settings
            tools, orients, colors = gather_properties(vals['-JSON-'])

            window['-TOOLS-'].update(
                values=tools,
                set_to_index=[tools.index(t) for t in sel_tools if t in tools]
            )
            window['-ORIENTS-'].update(
                values=orients,
                set_to_index=[orients.index(o) for o in sel_orients if o in orients]
            )

            # ignore the JSON’s colours and use your full master list
            final_colors = list(dict.fromkeys(parent.get(n, n) for n in centers))
            window['-COLORS-'].update(
                values=final_colors,
                set_to_index=[final_colors.index(c) for c in sel_colors if c in final_colors]
            )

            cfg['cache_enabled'] = vals['-CACHE-']
            cfg['thumb_size'] = int(vals['-SLIDER-'])
            cfg['min_score'] = float(vals['-MIN_SCORE-'])

            # store the ratio, then compute real area threshold:
            ratio = float(vals['-MIN_AREA-'])
            cfg['min_area'] = ratio

            save_config(cfg)

            # ── build a dynamic thumb_cache subdirectory based on filters + date ─────────
            parts = []
            if vals['-NO_WORDS-']:
                parts.append('no-text')
            else:
                if vals['-TOOLS-']:
                    parts.append('_'.join(vals['-TOOLS-']))
                if vals['-ORIENTS-']:
                    parts.append('_'.join(vals['-ORIENTS-']))
                if vals['-COLORS-']:
                    parts.append('_'.join(vals['-COLORS-']))
            parts.append(f"conf-{vals['-MIN_SCORE-']}")
            parts.append(f"size-{vals['-MIN_AREA-']}")
            if vals['-OVERLAY-'] and not vals['-NO_WORDS-']:
                parts.append('pred')
            date_str = datetime.now().strftime('%d.%m.%Y')
            parts.append(date_str)

            subdir_name = '_'.join(parts)
            cfg['cache_folder'] = str(SCRIPT_DIR / '.thumb_cache' / subdir_name)

            # make sure that folder exists
            Path(cfg['cache_folder']).mkdir(parents=True, exist_ok=True)
            worker_q = queue.Queue()
            worker_thread = threading.Thread(
                target=build_thumbnails,
                args=(worker_q, vals.copy(), cfg.copy()),
                daemon=True,
            )
            worker_thread.start()

        elif event == 'Save results':
            # nothing to do if no thumbnails
            if not thumbs:
                show_error('Nothing to copy')
                continue

            # ── initialize save-progress bar ─────────────────────────────
            total = len(thumbs)
            window['-PROG-'].update(current_count=0, max=total)
            window['-PCT-'].update(f"0/{total}")
            window.refresh()

            # choose either the user-picked folder or fallback to SCRIPT_DIR/output
            # out_dir = Path(vals['-OUT-']) if vals['-OUT-'] else (SCRIPT_DIR / 'output')
            # out_dir.mkdir(parents=True, exist_ok=True)

            # ── build a dynamic subdirectory name based on filters + date ─────────
            parts = []
            if vals['-NO_WORDS-']:
                parts.append('no-text')
            else:
                # include only non-empty selections
                if vals['-TOOLS-']:
                    parts.append('_'.join(vals['-TOOLS-']))
                if vals['-ORIENTS-']:
                    parts.append('_'.join(vals['-ORIENTS-']))
                if vals['-COLORS-']:
                    parts.append('_'.join(vals['-COLORS-']))
            parts.append(f"conf-{vals['-MIN_SCORE-']}")
            parts.append(f"size-{vals['-MIN_AREA-']}")
            # overlay only makes sense if there *are* words
            if vals['-OVERLAY-'] and not vals['-NO_WORDS-']:
                parts.append('pred')
            date_str = datetime.now().strftime('%d.%m.%Y')
            parts.append(date_str)

            subdir_name = '_'.join(parts)

            # choose base output folder (user-picked or default), then append our subdir
            base_out = Path(vals['-OUT-']) if vals['-OUT-'] else (SCRIPT_DIR / 'output')
            out_dir = base_out / subdir_name
            out_dir.mkdir(parents=True, exist_ok=True)

            # copy every image in thumbs into our stable folder
            for i, (_, full, anns) in enumerate(thumbs):
                dst = out_dir / Path(full).name
                if vals['-OVERLAY-']:
                    draw_overlay_and_save(full, dst, anns, cfg)

                else:
                    shutil.copy2(full, dst)

                # ── update save-progress bar ─────────────────────────────
                window['-PROG-'].update(current_count=i + 1)
                window['-PCT-'].update(f"{i + 1}/{total}")
                window.refresh()

            show_error('Done copying images!')

        elif event == 'Help':
            sg.popup(
                'Usage:\n• Pick JSON & image folders\n• Select visual properties\n• Filter & Show thumbnails\n• Save results …',
                title='Help')

        elif event == 'About':
            sg.popup(
                'ScriptSight\n'
                'Experimental beta version\n'
                '© 2025 Dr. Hussein Mohammed\n'
                'VMA lab at the CSMC',
                title='About'
            )

        elif event == '-HELP_SCORE-':
            sg.popup(
                "Set the minimum word-detection confidence required for the results to be considered. This number "
                "represents the model confidence score.",
                title="Help: Min. confidence"
            )
        elif event == '-HELP_AREA-':
            sg.popup(
                "Set the minimum word size required for the results to be considered. This number represents the "
                "ratio of the area covered by each word to the area of the largest word in that image.",
                title="Help: Min. word size"
            )


        elif event in key_to_thumb:
            full, anns = key_to_thumb[event]

            # if overlay mode, draw annotations on the full-res image
            if vals['-OVERLAY-']:
                import tempfile
                # create a temporary file for the overlay and remember its path
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    tmp_path = tmp.name
                draw_overlay_and_save(full, tmp_path,
                                      anns, cfg)  # uses width=5 and same label logic :contentReference[oaicite:0]{index=0}:contentReference[oaicite:1]{index=1}:contentReference[oaicite:2]{index=2}:contentReference[oaicite:3]{index=3}
                preview_path = tmp_path

            else:
                preview_path = full

            # load & resize for screen
            with Image.open(preview_path) as img:
                img = img.convert('RGB')
                screen_w = window.TKroot.winfo_screenwidth()
                screen_h = window.TKroot.winfo_screenheight()
                max_w, max_h = int(screen_w * 0.9), int(screen_h * 0.9)
                ratio = min(max_w / img.width, max_h / img.height, 1.0)

                if ratio < 1.0:
                    preview_img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                                             resample=Image.LANCZOS)
                else:
                    preview_img = img.copy()

            # display in a modal window
            buf = io.BytesIO()
            preview_img.save(buf, format='PNG')
            buf.seek(0)
            img_data = buf.getvalue()
            x = (screen_w - preview_img.width) // 2
            y = (screen_h - preview_img.height) // 2
            layout = [[sg.Image(data=img_data)], [sg.Button('Close')]]
            preview = sg.Window(f"Preview {Path(preview_path).name}",
                                layout,
                                resizable=True,
                                finalize=True,
                                location=(x, y))
            while True:
                e, _ = preview.read()
                if e in (sg.WIN_CLOSED, 'Close'):
                    break
            preview.close()
            if vals['-OVERLAY-']:
                os.unlink(preview_path)

    window.close()


if __name__ == '__main__':
    main()
