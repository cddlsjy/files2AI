#!/usr/bin/env python3
"""
项目代码处理器（最终版）
- 输出文件名为 <项目名>-AI.txt
- 输入输出目录自动同步
- 代码、JSON、PDF、Word 分别控制，默认仅代码勾选
- 自定义扩展名（逗号分隔）自动处理
- 自动处理与记忆功能
"""

import os
import sys
import zipfile
import threading
import time
import subprocess
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# 第三方库（可选）
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
try:
    import docx
except ImportError:
    docx = None
try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import pytesseract
except ImportError:
    pytesseract = None
try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

# 文件扩展名
CODE_EXTENSIONS = {
    '.java', '.kt', '.kts', '.ets', '.c', '.h', '.cpp', '.cc', '.cxx', '.hpp', '.hxx',
    '.py', '.js', '.ts', '.go', '.rs', '.swift', '.rb', '.php', '.sql'
}
JSON_EXTENSIONS = {'.json'}
PDF_EXTENSIONS = {'.pdf'}
WORD_EXTENSIONS = {'.doc', '.docx'}

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_processor_config.json")

# ---------- 工具类 ----------
class SplitFileWriter:
    """自动分卷写入文本文件"""
    def __init__(self, base_path, max_bytes):
        self.base_path = base_path
        self.max_bytes = max_bytes
        self.part = 1
        self.current_file = None
        self.current_size = 0
        self._open_new_file()

    def _open_new_file(self):
        if self.current_file:
            self.current_file.close()
        if self.part == 1:
            filename = self.base_path
        else:
            base, ext = os.path.splitext(self.base_path)
            filename = f"{base}_part{self.part}{ext}"
        self.current_file = open(filename, 'w', encoding='utf-8')
        self.current_size = 0

    def _check_rotate(self, additional_bytes):
        if self.current_size + additional_bytes > self.max_bytes and self.max_bytes > 0:
            self.part += 1
            self._open_new_file()

    def write(self, data):
        bytes_len = len(data.encode('utf-8'))
        self._check_rotate(bytes_len)
        self.current_file.write(data)
        self.current_file.flush()
        self.current_size += bytes_len

    def close(self):
        if self.current_file:
            self.current_file.close()

def get_tree_structure(file_list):
    """从文件列表生成目录树"""
    if not file_list:
        return "(empty)"
    tree = {}
    for path in file_list:
        parts = path.split('/')
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
    lines = []
    def walk(node, indent=''):
        items = sorted(node.items())
        for i, (name, child) in enumerate(items):
            is_last = (i == len(items)-1)
            prefix = '└── ' if is_last else '├── '
            lines.append(f"{indent}{prefix}{name}")
            if child:
                walk(child, indent + ('    ' if is_last else '│   '))
    walk(tree)
    return '\n'.join(lines)

def safe_read_text(zip_file, info):
    """从ZIP安全读取文本"""
    try:
        with zip_file.open(info) as f:
            return f.read().decode('utf-8')
    except UnicodeDecodeError:
        with zip_file.open(info) as f:
            return f.read().decode('latin-1', errors='replace')

def extract_pdf_text(pdf_path, use_ocr=False):
    """提取PDF文本，可选OCR"""
    text = ""
    if not use_ocr and PyPDF2:
        try:
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            return text
        except Exception as e:
            text = f"[PDF提取错误: {e}]\n"
            if use_ocr and pytesseract and convert_from_path:
                use_ocr = True
            else:
                return text
    if use_ocr and pytesseract and convert_from_path:
        try:
            images = convert_from_path(pdf_path)
            for i, img in enumerate(images):
                ocr_text = pytesseract.image_to_string(img, lang='chi_sim+eng')
                text += f"[页面 {i+1}]\n{ocr_text}\n"
        except Exception as e:
            text += f"[OCR错误: {e}]\n"
    elif use_ocr:
        text += "[OCR不可用，请安装pytesseract和pdf2image]\n"
    return text

def extract_word_text(doc_path, use_ocr=False):
    """提取Word文本，可选OCR图片"""
    text = ""
    if docx:
        try:
            doc = docx.Document(doc_path)
            for para in doc.paragraphs:
                text += para.text + "\n"
            if use_ocr and pytesseract:
                for rel in doc.part.rels.values():
                    if "image" in rel.target_ref:
                        try:
                            image_data = rel.target_part.blob
                            from io import BytesIO
                            img = Image.open(BytesIO(image_data))
                            ocr_text = pytesseract.image_to_string(img, lang='chi_sim+eng')
                            text += f"[图片OCR:]\n{ocr_text}\n"
                        except:
                            pass
            return text
        except Exception as e:
            return f"[Word提取错误: {e}]\n"
    else:
        return "[python-docx未安装，无法处理Word文件]\n"

def process_input(input_path, output_dir, max_bytes, process_code, process_json, process_pdf, process_word, use_ocr, custom_exts, log_callback, progress_callback, stop_flag):
    """主处理函数（后台线程）"""
    try:
        # 判断输入类型
        is_zip = False
        if os.path.isfile(input_path) and input_path.lower().endswith('.zip'):
            is_zip = True
            zip_file = zipfile.ZipFile(input_path, 'r')
            entries = [info for info in zip_file.infolist() if not info.is_dir()]
            file_paths = [info.filename for info in entries]
        else:
            # 目录
            file_paths = []
            for root, dirs, files in os.walk(input_path):
                for f in files:
                    rel_path = os.path.relpath(os.path.join(root, f), input_path)
                    file_paths.append(rel_path)
            entries = None

        if not file_paths:
            log_callback("输入为空")
            return

        # 分类
        code_files = []
        json_files = []
        pdf_files = []
        word_files = []
        custom_files = []
        for path in file_paths:
            ext = os.path.splitext(path)[1].lower()
            if process_code and ext in CODE_EXTENSIONS:
                code_files.append(path)
            elif process_json and ext in JSON_EXTENSIONS:
                json_files.append(path)
            elif process_pdf and ext in PDF_EXTENSIONS:
                pdf_files.append(path)
            elif process_word and ext in WORD_EXTENSIONS:
                word_files.append(path)
            elif custom_exts and ext in custom_exts:
                custom_files.append(path)

        all_files = code_files + json_files + pdf_files + word_files + custom_files
        if not all_files:
            log_callback("没有符合条件的文件")
            return

        log_callback(f"扫描完成: 代码{len(code_files)}个, JSON{len(json_files)}个, PDF{len(pdf_files)}个, Word{len(word_files)}个, 自定义{len(custom_files)}个")

        # 生成目录树
        tree_str = get_tree_structure(file_paths)

        # 输出文件基础名（改为 <项目名>-AI.txt）
        base_name = os.path.splitext(os.path.basename(input_path))[0] or "project"
        output_base = os.path.join(output_dir, f"{base_name}-AI.txt")

        # 写入器
        writer = SplitFileWriter(output_base, max_bytes)

        # 写入目录树
        writer.write("=" * 80 + "\n")
        writer.write("项目目录树\n")
        writer.write("=" * 80 + "\n\n")
        writer.write(tree_str)
        writer.write("\n\n")

        def write_section(title, file_list, reader_func, lang_hint='text'):
            if not file_list:
                return
            writer.write("=" * 80 + "\n")
            writer.write(title + "\n")
            writer.write("=" * 80 + "\n\n")
            for idx, path in enumerate(file_list):
                if stop_flag.is_set():
                    log_callback("处理已停止")
                    writer.close()
                    return
                progress_callback(idx+1, len(file_list))
                log_callback(f"处理: {path}")
                writer.write(f"## 文件: {path}\n")
                ext = os.path.splitext(path)[1].lower()
                lang = ext[1:] if ext else lang_hint
                if lang in ('pdf', 'doc', 'docx'):
                    lang = 'text'
                writer.write(f"```{lang}\n")
                content = reader_func(path)
                writer.write(content)
                if not content.endswith('\n'):
                    writer.write('\n')
                writer.write("```\n\n")

        # 代码文件
        if code_files:
            def read_code(path):
                if is_zip:
                    entry = next((e for e in entries if e.filename == path), None)
                    if entry:
                        return safe_read_text(zip_file, entry)
                else:
                    full = os.path.join(input_path, path)
                    try:
                        with open(full, 'r', encoding='utf-8') as f:
                            return f.read()
                    except UnicodeDecodeError:
                        with open(full, 'r', encoding='latin-1', errors='replace') as f:
                            return f.read()
                return "[无法读取]"
            write_section("代码文件内容", code_files, read_code)

        # JSON文件
        if json_files:
            def read_json(path):
                if is_zip:
                    entry = next((e for e in entries if e.filename == path), None)
                    if entry:
                        return safe_read_text(zip_file, entry)
                else:
                    full = os.path.join(input_path, path)
                    try:
                        with open(full, 'r', encoding='utf-8') as f:
                            return f.read()
                    except:
                        return "[读取错误]"
            write_section("JSON文件内容", json_files, read_json, 'json')

        # PDF文件
        if pdf_files:
            def read_pdf(path):
                if is_zip:
                    entry = next((e for e in entries if e.filename == path), None)
                    if entry:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                            tmp.write(zip_file.read(entry))
                            tmp_path = tmp.name
                        text = extract_pdf_text(tmp_path, use_ocr)
                        os.unlink(tmp_path)
                        return text
                else:
                    full = os.path.join(input_path, path)
                    return extract_pdf_text(full, use_ocr)
                return "[无法读取PDF]"
            write_section("PDF文件内容", pdf_files, read_pdf)

        # Word文件
        if word_files:
            def read_word(path):
                if is_zip:
                    entry = next((e for e in entries if e.filename == path), None)
                    if entry:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
                            tmp.write(zip_file.read(entry))
                            tmp_path = tmp.name
                        text = extract_word_text(tmp_path, use_ocr)
                        os.unlink(tmp_path)
                        return text
                else:
                    full = os.path.join(input_path, path)
                    return extract_word_text(full, use_ocr)
                return "[无法读取Word]"
            write_section("Word文件内容", word_files, read_word)

        # 自定义扩展名文件
        if custom_files:
            def read_custom(path):
                if is_zip:
                    entry = next((e for e in entries if e.filename == path), None)
                    if entry:
                        return safe_read_text(zip_file, entry)
                else:
                    full = os.path.join(input_path, path)
                    try:
                        with open(full, 'r', encoding='utf-8') as f:
                            return f.read()
                    except UnicodeDecodeError:
                        with open(full, 'r', encoding='latin-1', errors='replace') as f:
                            return f.read()
                return "[无法读取]"
            write_section("自定义文件内容", custom_files, read_custom)

        writer.close()
        log_callback(f"处理完成！输出文件: {output_base}" + (f" (及分卷)" if writer.part > 1 else ""))
        return output_dir  # 返回输出目录，用于打开

    except Exception as e:
        log_callback(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if is_zip and 'zip_file' in locals():
            zip_file.close()

# ---------- GUI 应用 ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("项目代码处理器")
        self.geometry("780x680")
        self.resizable(True, True)

        # 变量
        self.input_type = tk.StringVar(value="zip")  # "zip" 或 "dir"
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar(value=os.path.dirname(os.path.abspath(__file__)))
        self.split_mb = tk.IntVar(value=10)
        self.process_code = tk.BooleanVar(value=True)
        self.process_json = tk.BooleanVar(value=False)   # JSON 默认不选
        self.process_pdf = tk.BooleanVar(value=False)    # PDF 默认不选
        self.process_word = tk.BooleanVar(value=False)   # Word 默认不选
        self.use_ocr = tk.BooleanVar(value=False)
        self.custom_exts = tk.StringVar()                # 用户自定义扩展名
        self.auto_process = tk.BooleanVar(value=True)    # 自动处理，默认开启

        self.stop_flag = threading.Event()
        self.thread = None
        self.processing = False

        # 加载配置
        self.load_config()

        # 创建界面
        self.create_widgets()
        self.update_idletasks()

    def create_widgets(self):
        # 输入区域
        frame_input = tk.LabelFrame(self, text="输入源", padx=5, pady=5)
        frame_input.pack(fill='x', padx=10, pady=5)

        # 类型选择
        type_frame = tk.Frame(frame_input)
        type_frame.pack(fill='x', pady=2)
        tk.Radiobutton(type_frame, text="ZIP 文件", variable=self.input_type, value="zip").pack(side='left', padx=5)
        tk.Radiobutton(type_frame, text="文件夹", variable=self.input_type, value="dir").pack(side='left', padx=5)

        # 路径选择
        path_frame = tk.Frame(frame_input)
        path_frame.pack(fill='x', pady=2)
        self.input_entry = tk.Entry(path_frame, textvariable=self.input_path, width=50)
        self.input_entry.pack(side='left', fill='x', expand=True)
        tk.Button(path_frame, text="浏览...", command=self.browse_input).pack(side='right', padx=5)

        # 输出目录（只读显示，但可手动修改）
        frame_output = tk.LabelFrame(self, text="输出目录", padx=5, pady=5)
        frame_output.pack(fill='x', padx=10, pady=5)
        self.output_entry = tk.Entry(frame_output, textvariable=self.output_dir, width=50)
        self.output_entry.pack(side='left', fill='x', expand=True)
        tk.Button(frame_output, text="浏览...", command=self.browse_output).pack(side='right', padx=5)

        # 分割大小滑块
        frame_split = tk.LabelFrame(self, text="单个文件大小限制 (MB, 0=不分割)", padx=5, pady=5)
        frame_split.pack(fill='x', padx=10, pady=5)
        self.split_slider = tk.Scale(frame_split, from_=0, to=100, orient='horizontal', variable=self.split_mb, length=300)
        self.split_slider.pack(side='left', fill='x', expand=True)
        self.split_label = tk.Label(frame_split, text="10 MB")
        self.split_label.pack(side='right', padx=5)
        self.split_mb.trace_add('write', lambda *a: self.split_label.config(text=f"{self.split_mb.get()} MB"))

        # 处理选项
        frame_options = tk.LabelFrame(self, text="处理选项", padx=5, pady=5)
        frame_options.pack(fill='x', padx=10, pady=5)

        # 第一行：代码、JSON、PDF、Word
        row1 = tk.Frame(frame_options)
        row1.pack(fill='x', pady=2)
        tk.Checkbutton(row1, text="代码文件", variable=self.process_code).pack(side='left', padx=5)
        tk.Checkbutton(row1, text="JSON文件", variable=self.process_json).pack(side='left', padx=5)
        tk.Checkbutton(row1, text="PDF文件", variable=self.process_pdf).pack(side='left', padx=5)
        tk.Checkbutton(row1, text="Word文件", variable=self.process_word).pack(side='left', padx=5)

        # 第二行：OCR + 自定义扩展名
        row2 = tk.Frame(frame_options)
        row2.pack(fill='x', pady=2)
        tk.Checkbutton(row2, text="启用OCR (图片文字识别)", variable=self.use_ocr).pack(side='left', padx=5)
        tk.Label(row2, text="自定义扩展名 (逗号分隔，例如 .yml,.toml):").pack(side='left', padx=5)
        tk.Entry(row2, textvariable=self.custom_exts, width=30).pack(side='left', padx=5)

        # 自动处理与手动按钮
        frame_control = tk.Frame(self)
        frame_control.pack(fill='x', padx=10, pady=5)
        self.auto_cb = tk.Checkbutton(frame_control, text="自动处理 (选择输入后立即开始)", variable=self.auto_process)
        self.auto_cb.pack(side='left', padx=5)
        self.start_btn = tk.Button(frame_control, text="▶ 开始处理", command=self.start_process, width=12)
        self.start_btn.pack(side='left', padx=5)
        self.stop_btn = tk.Button(frame_control, text="⏹ 停止", command=self.stop_process, state='disabled', width=12)
        self.stop_btn.pack(side='left', padx=5)
        self.clear_btn = tk.Button(frame_control, text="🗑 清空日志", command=self.clear_log, width=12)
        self.clear_btn.pack(side='left', padx=5)

        # 进度条和状态
        frame_progress = tk.Frame(self)
        frame_progress.pack(fill='x', padx=10, pady=5)
        self.progress_bar = ttk.Progressbar(frame_progress, orient='horizontal', length=400, mode='determinate')
        self.progress_bar.pack(side='left', fill='x', expand=True)
        self.status_label = tk.Label(frame_progress, text="就绪", width=15)
        self.status_label.pack(side='right', padx=5)
        self.detail_label = tk.Label(self, text="")
        self.detail_label.pack(padx=10)

        # 日志区域
        log_frame = tk.LabelFrame(self, text="日志", padx=5, pady=5)
        log_frame.pack(fill='both', expand=True, padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state='normal')
        self.log_text.pack(fill='both', expand=True)

        # 初始时禁止停止按钮
        self.update_ui_state()

        # 绑定关闭事件，保存配置
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def browse_input(self):
        """根据选择的类型打开文件/文件夹选择器"""
        if self.processing:
            messagebox.showinfo("提示", "正在处理中，请稍后...")
            return
        if self.input_type.get() == "zip":
            path = filedialog.askopenfilename(filetypes=[("ZIP files", "*.zip")])
        else:
            path = filedialog.askdirectory()
        if path:
            self.input_path.set(path)
            # 自动设置输出目录为输入源的父目录（ZIP文件）或目录本身（文件夹）
            if self.input_type.get() == "zip":
                new_output = os.path.dirname(path)
            else:
                new_output = path
            self.output_dir.set(new_output)
            # 如果自动处理被勾选，立即开始处理
            if self.auto_process.get():
                self.start_process()

    def browse_output(self):
        path = filedialog.askdirectory()
        if path:
            self.output_dir.set(path)

    def log(self, msg):
        timestamp = time.strftime("[%H:%M:%S]")
        self.log_text.insert('end', f"{timestamp} {msg}\n")
        self.log_text.see('end')
        self.update_idletasks()

    def clear_log(self):
        self.log_text.delete(1.0, 'end')

    def update_progress(self, current, total):
        percent = int(current * 100 / total) if total > 0 else 0
        self.progress_bar['value'] = percent
        self.detail_label.config(text=f"文件: {current}/{total}")
        self.update_idletasks()

    def update_ui_state(self):
        """根据处理状态启用/禁用控件"""
        if self.processing:
            self.start_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
            # 输入相关控件可设置为只读
            self.input_entry.config(state='readonly')
            self.output_entry.config(state='readonly')
        else:
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            self.input_entry.config(state='normal')
            self.output_entry.config(state='normal')

    def start_process(self):
        """开始处理（手动或自动触发）"""
        if self.processing:
            return
        input_path = self.input_path.get().strip()
        if not input_path:
            messagebox.showwarning("警告", "请先选择输入源")
            return
        output_dir = self.output_dir.get().strip()
        if not output_dir:
            # 如果输出目录为空，回退到脚本所在目录
            output_dir = os.path.dirname(os.path.abspath(__file__))
            self.output_dir.set(output_dir)

        # 验证输入路径存在
        if not os.path.exists(input_path):
            messagebox.showerror("错误", "输入路径不存在")
            return

        max_bytes = self.split_mb.get() * 1024 * 1024 if self.split_mb.get() > 0 else 0

        # 解析自定义扩展名
        custom_exts_str = self.custom_exts.get().strip()
        custom_exts = set()
        if custom_exts_str:
            for ext in custom_exts_str.split(','):
                ext = ext.strip().lower()
                if ext and ext.startswith('.'):
                    custom_exts.add(ext)
                elif ext and not ext.startswith('.'):
                    custom_exts.add('.' + ext)  # 自动补点
                # 忽略空字符串

        # 清空停止标志
        self.stop_flag.clear()
        self.processing = True
        self.update_ui_state()

        # 重置进度
        self.progress_bar['value'] = 0
        self.detail_label.config(text="")
        self.log("开始处理...")

        # 启动后台线程
        self.thread = threading.Thread(
            target=self.process_worker,
            args=(input_path, output_dir, max_bytes, custom_exts),
            daemon=True
        )
        self.thread.start()
        # 监控线程结束
        self.after(100, self.check_thread)

    def process_worker(self, input_path, output_dir, max_bytes, custom_exts):
        """后台处理函数"""
        result_dir = process_input(
            input_path, output_dir, max_bytes,
            self.process_code.get(), self.process_json.get(),
            self.process_pdf.get(), self.process_word.get(),
            self.use_ocr.get(),
            custom_exts,
            self.log, self.update_progress, self.stop_flag
        )
        # 处理完成后，如果成功且没有停止，打开输出目录
        if result_dir and not self.stop_flag.is_set():
            self.open_folder(result_dir)

    def open_folder(self, folder):
        """跨平台打开文件夹"""
        try:
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder])
            else:
                subprocess.run(['xdg-open', folder])
        except Exception as e:
            self.log(f"无法打开文件夹: {e}")

    def check_thread(self):
        if self.thread and self.thread.is_alive():
            self.after(100, self.check_thread)
        else:
            self.processing = False
            self.update_ui_state()
            self.log("处理结束")

    def stop_process(self):
        self.stop_flag.set()
        self.log("正在停止...")

    def load_config(self):
        """加载配置文件"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                # 恢复设置
                self.input_type.set(cfg.get("input_type", "zip"))
                self.input_path.set(cfg.get("input_path", ""))
                self.output_dir.set(cfg.get("output_dir", os.path.dirname(os.path.abspath(__file__))))
                self.split_mb.set(cfg.get("split_mb", 10))
                self.process_code.set(cfg.get("process_code", True))
                self.process_json.set(cfg.get("process_json", False))
                self.process_pdf.set(cfg.get("process_pdf", False))
                self.process_word.set(cfg.get("process_word", False))
                self.use_ocr.set(cfg.get("use_ocr", False))
                self.custom_exts.set(cfg.get("custom_exts", ""))
                self.auto_process.set(cfg.get("auto_process", True))
            except Exception as e:
                print(f"加载配置失败: {e}")

    def save_config(self):
        """保存配置到文件"""
        cfg = {
            "input_type": self.input_type.get(),
            "input_path": self.input_path.get(),
            "output_dir": self.output_dir.get(),
            "split_mb": self.split_mb.get(),
            "process_code": self.process_code.get(),
            "process_json": self.process_json.get(),
            "process_pdf": self.process_pdf.get(),
            "process_word": self.process_word.get(),
            "use_ocr": self.use_ocr.get(),
            "custom_exts": self.custom_exts.get(),
            "auto_process": self.auto_process.get(),
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置失败: {e}")

    def on_closing(self):
        """窗口关闭时保存配置"""
        self.save_config()
        self.destroy()

if __name__ == "__main__":
    app = App()
    app.mainloop()
