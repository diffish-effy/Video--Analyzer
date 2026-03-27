import streamlit as st
import google.generativeai as genai
import tempfile
import subprocess
import time
import os
import yt_dlp

# 通过环境变量读取 Gemini 3 API Key
API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=API_KEY)

st.title("🎬 行业级视频深度分析")

# 强制读取 PROXY_URL 配置
PROXY_URL = ""
try:
    PROXY_URL = st.secrets["PROXY_URL"]
except Exception:
    PROXY_URL = ""

# 选择来源（本地 or 网络链接）
mode = st.sidebar.radio("选择来源", ["上传本地视频", "输入视频链接"])

# 用 session_state 管理状态，便于数据隔离
if "video_path" not in st.session_state:
    st.session_state.video_path = None
if "analyzed" not in st.session_state:
    st.session_state.analyzed = False
if "analysis_response" not in st.session_state:
    st.session_state.analysis_response = None

def cleanup_temp_files():
    # 删除所有临时或残留 .mp4/.mkv/.webm 文件
    wildcards = ["*.mp4", "*.mkv", "*.webm"]
    for pattern in wildcards:
        for fname in [f for f in os.listdir(".") if f.endswith(pattern.split("*")[-1])]:
            try:
                os.remove(fname)
            except Exception:
                pass
    for fname in ["temp_video.mp4", "downloaded_raw.mp4", "downloaded_raw.mkv", "downloaded_raw.webm"]:
        try:
            if os.path.exists(fname):
                os.remove(fname)
        except Exception:
            pass

video_path = None

def is_xiaohongshu_shortlink(url):
    # 检查是否为小红书短链
    return "xhslink.com" in url

def resolve_xhslink(url):
    import requests
    try:
        resp = requests.head(url, allow_redirects=True, timeout=8)
        return resp.url
    except Exception:
        return url

def gen_headers(url, ua_code=0):
    """通用 Headers 自动配置。支持多UA类型和平台Referer。"""
    uas = [
        # 安卓端 Chrome UA
        "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.77 Mobile Safari/537.36",
        # 安卓端夸克
        "Mozilla/5.0 (Linux; Android 10; ELS-AN00 Build/HUAWEIELS-AN00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/81.0.4044.117 Mobile Safari/537.36 Quark/6.4.2.208"
    ]
    ua = uas[ua_code % len(uas)]
    # 强力 referer 模拟
    if "xiaohongshu" in url or "xhslink" in url:
        referer = "https://www.xiaohongshu.com/"
    elif "youtube" in url or "youtu.be" in url:
        referer = "https://www.google.com/"
    elif "bilibili" in url:
        referer = "https://www.bilibili.com/"
    else:
        referer = "https://www.google.com/"
    return {
        'User-Agent': ua,
        'Referer': referer
    }

import glob

def download_video_with_ytdlp(url, output_stub):
    # 文件预清理：强力删除所有 temp_video 开头的文件，不管后缀
    for f in glob.glob("temp_video*"):
        try:
            os.remove(f)
        except Exception:
            pass
    # 清理其它已知残留文件
    cleanup_temp_files()

    max_retries = 2

    try:
        with st.spinner("正在请求视频下载任务..."):
            st.toast(f"尝试抓取 URL: {url}")
            start_time = time.time()
            last_error_detail = None  # 双重保险，记录详细异常
            for retry in range(max_retries):
                headers = gen_headers(url, ua_code=retry)

                # 极致省钱加速：强制拉最低码率低清晰度，优先 mp4
                ydl_opts = {
                    'format': 'worst[ext=mp4]/mp4/worst',  # 强制最低清晰度、仅mp4优先
                    'merge_output_format': 'mp4',
                    'outtmpl': output_stub + '.%(ext)s',
                    'ignoreerrors': True,
                    'no_warnings': True,
                    'nocheckcertificate': True,
                    'quiet': True,
                    'noprogress': True,
                    'no_color': True,
                    'http_headers': headers,
                    'external_downloader_args': [
                        '-headers', f"User-Agent: {headers['User-Agent']}",
                        '-headers', f"Referer: {headers['Referer']}"
                    ],
                    'skip_download': False,
                    'prefer_ffmpeg': True,
                    'force_generic_extractor': False,
                    'allow_unplayable_formats': False,
                    'geo_bypass': True,
                    'source_address': None,
                    'postprocessors': [],
                    'no_playlist': True,
                    'force_no_dash': True,
                    'force_no_hls': True,
                    # 安卓端全协议适配&强制B站国际
                    'extractor_args': {
                        'youtube': {'player_client': ['android']},
                        'bilibili': {'prefer_intl': True},
                    },
                    'outtmpl_na_placeholder': '-NA-',
                    'socket_timeout': 30,
                    'retries': 2,
                }

                # 注入代理（如果已配置）
                if PROXY_URL:
                    ydl_opts['proxy'] = PROXY_URL

                st.toast(f"第{retry+1}次尝试: 正在请求流媒体信息...", icon="🔗")

                try:
                    # 设置下载的超时警告和轮询进度
                    progress_interval = 5
                    next_report = start_time + progress_interval

                    def progress_hook(d):
                        nonlocal next_report
                        if time.time() >= next_report:
                            st.toast(f"下载状态: {d.get('status', '')}, {d.get('filename', '')}", icon="⏳")
                            next_report = time.time() + progress_interval

                    ydl_opts['progress_hooks'] = [progress_hook]

                    # 强制超时机制：30秒无响应直接报错
                    import threading
                    err_holder = []
                    def do_download():
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                ydl.download([url])
                        except Exception as ee:
                            err_holder.append(ee)

                    t = threading.Thread(target=do_download)
                    t.daemon = True
                    t.start()
                    t.join(timeout=30)
                    if t.is_alive():
                        st.toast("下载超时：超出30秒未响应，强行中止", icon="⏳")
                        raise TimeoutError("下载超时：30秒未完成")
                    if err_holder:
                        raise err_holder[0]

                except yt_dlp.utils.DownloadError as e:
                    last_error_detail = str(e)
                    st.toast(f"DownloadError: {last_error_detail}", icon="⚠️")
                    # 抓403等原始错误码
                    if "HTTP Error 403" in last_error_detail and retry == 0:
                        continue
                    raise e
                except Exception as ee:
                    last_error_detail = str(ee)
                    st.toast(f"Exception: {last_error_detail}", icon="⚠️")
                    raise ee

                # 检查合并后标准mp4
                for ext in ["mp4"]:
                    candidate = f"{output_stub}.{ext}"
                    if os.path.exists(candidate):
                        st.toast(f"文件抓取成功: {candidate}", icon="✅")
                        return candidate
            # 捕获所有失败异常原因，明显显示给用户定位（流量/代理/etc.）
            msg = "所有尝试均未能下载到视频文件"
            if last_error_detail:
                msg += f"\n错误详情: {last_error_detail}\n请核查代理状态及流量是否足够。"
            raise Exception(msg)
    except Exception as e:
        # 页面上直接展示错误细节，方便快速定位流量/代理问题
        st.error(str(e))
        st.code(str(e))
        return None

def brutal_auto_grab_and_preview(url):
    """
    自动全协议适配暴力抓取所有主流平台短链/原链
    1. 解析小红书短链
    2. 按 Headers+UA 各种重试暴力直至成功
    """
    # 小红书短链兼容
    test_urls = []
    real_url = url
    if is_xiaohongshu_shortlink(url):
        real_url = resolve_xhslink(url)
        test_urls.append(real_url)
    else:
        test_urls.append(url)
    if real_url != url:
        test_urls.append(url)
    output_stub = "downloaded_raw"
    for _url in test_urls:
        found = download_video_with_ytdlp(_url, output_stub)
        if found:
            # ffmpeg 强制转为 720p mp4（防止源头高分溢出），视频已极致瘦身
            tmp_mp4 = "temp_video.mp4"
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", found, "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease", "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-c:a", "aac", "-movflags", "+faststart", tmp_mp4
            ]
            proc_ffmpeg = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if proc_ffmpeg.returncode == 0 and os.path.exists(tmp_mp4):
                st.toast("转码成功，准备展示", icon="✅")
                return tmp_mp4
    return None

if mode == "上传本地视频":
    uploaded_file = st.file_uploader("选择本地视频文件", type=["mp4", "mov"])
    if uploaded_file and st.button("预览本地视频", key="local_preview_btn"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(uploaded_file.read())
            video_path = tmp.name
        st.session_state.video_path = video_path
        st.session_state.analyzed = False
        st.session_state.analysis_response = None
        st.video(video_path)
    elif st.session_state.video_path and os.path.exists(st.session_state.video_path):
        st.video(st.session_state.video_path)

elif mode == "输入视频链接":
    video_url = st.text_input("请输入要分析的视频链接（如YouTube、B站、xhs等）：")
    if video_url:
        with st.status("🚀 正在调动全球节点抓取并解析视频...", expanded=True):
            cleanup_temp_files()
            grabbed_mp4 = brutal_auto_grab_and_preview(video_url)
            if grabbed_mp4 and os.path.exists(grabbed_mp4):
                video_path = grabbed_mp4
                st.session_state.video_path = grabbed_mp4
                st.success("抓取 OK！上车预览！")
                st.video(video_path)
                st.session_state.analyzed = False
                st.session_state.analysis_response = None
            else:
                # 抓取异常时由 download_video_with_ytdlp 透传异常，不再沉默
                st.session_state.video_path = None
    elif st.session_state.video_path and os.path.exists(st.session_state.video_path):
        st.video(st.session_state.video_path)

# 保证路径合法性（防止 404）
if st.session_state.video_path and os.path.exists(st.session_state.video_path):
    if st.button("开始行业深度审美分析", key="analyze_btn"):
        try:
            with st.status("Gemini 3 正在深入扫描..."):
                my_file = genai.upload_file(path=st.session_state.video_path)
                while my_file.state.name == "PROCESSING":
                    time.sleep(2)
                    my_file = genai.get_file(my_file.name)
                prompt = (
                    "你是一名顶尖资深媒体分析师。请严格按照以下维度对该视频进行行业级深度分析，权重极度侧重于【类型定性】:\n\n"
                    "第一部分【行业类型定性】（Top Priority 必须首先展示）:\n"
                    "- 从【剧情短片、TVC/商业广告、文艺/实验片、纪录片、动画短片、MV、综艺剪辑、品牌ID】这8个大类中，明确甄别本视频最适合归属的唯一行业类型，并用专业术语写明。\n"
                    "- 进一步细化行业称谓，精准给出细分定义（如“二维手绘治愈系动画”、“新春品牌视觉ID”、“意识流实验短片”等），要求短语与行业术语接轨，具有辨识度。\n\n"
                    "第二部分【深度审美分析】（以分段形式，每部分明确小标题）：\n"
                    "1. 【叙事风格】：不仅考察叙事节奏，更需聚焦其叙述策略（如线性叙事、碎片意象、情绪驱动等），必要时解析引用的象征意象与隐喻表达。\n"
                    "2. 【影像视觉】：剖析本片影像美学源流（如极简主义、表现主义、赛博朋克等），说明具体的画面构图、调色及镜头运动等手段。\n"
                    "3. 【音效特效】：判断声音（配乐、Foley等）与所归属的类型之间的关联性，如环境音如何塑造电影感、配乐是否强化商业属性、音画协同的感官导向。\n\n"
                    "最后一个分段【类型归因的论证】:\n"
                    "- 根据你对视听细节的系统分析，反向论证为什么该视频归为第一部分选定的行业类型，并用行业视角有说服力地说明理由。\n\n"
                    "整体分析请以专业、锐利并富有行业高度的语言表述，避免平铺直叙。"
                )
                model = genai.GenerativeModel("gemini-3-flash-preview")
                response = model.generate_content([prompt, my_file])
            st.session_state.analysis_response = response.text
            st.session_state.analyzed = True
            st.session_state.video_path = None
        except Exception as ee:
            # 报告分析阶段异常
            st.error(f"AI分析阶段出错：{str(ee)}")

    if st.session_state.analyzed and st.session_state.analysis_response:
        st.subheader("📋 行业深度审美报告")
        st.markdown(st.session_state.analysis_response)
        st.session_state.analyzed = False
        st.session_state.analysis_response = None

else:
    if st.session_state.video_path and not os.path.exists(st.session_state.video_path):
        # 静默跳过404
        st.session_state.video_path = None