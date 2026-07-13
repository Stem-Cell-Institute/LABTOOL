import os
import time
import threading
from google import genai
from dotenv import load_dotenv

# 1. Load cấu hình
load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

if not api_key:
    print("LỖI: Không tìm thấy GOOGLE_API_KEY trong file .env")
    exit()

# 2. Khởi tạo client Gemini (SDK mới: google-genai, thay cho google-generativeai đã hết hỗ trợ)
client = genai.Client(api_key=api_key)

# Model còn hoạt động (gemini-1.5-flash / gemini-1.5-pro đã bị gỡ khỏi API, gây lỗi 404)
MODEL_NAME = "gemini-2.5-flash"

folder_path = "D:/Lab_Data/Videos"

if not os.path.exists(folder_path):
    print(f"LỖI: Thư mục {folder_path} không tồn tại!")
    exit()


# ============== SPINNER: báo hiệu app vẫn đang chạy trong lúc chờ ==============

SPINNER_FRAMES = ["|", "/", "-", "\\"]

def _clear_line():
    """Xóa sạch dòng hiện tại trên console (để xóa icon xoay trước khi in nội dung mới)."""
    print("\r" + " " * 80 + "\r", end="", flush=True)

def spin_sleep(seconds, message):
    """Ngủ 'seconds' giây nhưng vẫn hiện icon xoay |/-\\ để biết app chưa bị đứng."""
    steps = max(int(seconds / 0.2), 1)
    for i in range(steps):
        print(f"\r{message}... {SPINNER_FRAMES[i % 4]}", end="", flush=True)
        time.sleep(0.2)
    _clear_line()

def run_with_spinner(func, message="Đang xử lý"):
    """Chạy func() trong thread riêng, hiện icon xoay để báo app vẫn đang hoạt động (không bị treo)."""
    box = {}

    def worker():
        try:
            box["value"] = func()
        except Exception as e:
            box["error"] = e

    thread = threading.Thread(target=worker)
    thread.start()

    i = 0
    while thread.is_alive():
        print(f"\r{message}... {SPINNER_FRAMES[i % 4]}", end="", flush=True)
        time.sleep(0.2)
        i += 1

    thread.join()
    _clear_line()

    if "error" in box:
        raise box["error"]
    return box["value"]


# ============== GỌI GEMINI (có retry khi quá tải tạm thời) ==============

def call_gemini_with_retry(contents, max_retries=3, wait_seconds=15):
    """Gọi Gemini (có hiện spinner trong lúc chờ), tự động thử lại nếu gặp lỗi tạm thời (503 quá tải, 429 vượt rate limit...)."""
    for attempt in range(1, max_retries + 1):
        try:
            return run_with_spinner(
                lambda: client.models.generate_content(model=MODEL_NAME, contents=contents),
                message="Đang chờ Gemini trả lời",
            )
        except Exception as e:
            error_text = str(e)
            la_loi_tam_thoi = any(
                ma in error_text
                for ma in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded"]
            )
            if la_loi_tam_thoi and attempt < max_retries:
                cho = wait_seconds * attempt
                print(f"\n⚠️  Gemini đang quá tải tạm thời (lần {attempt}/{max_retries}), thử lại sau {cho}s...")
                spin_sleep(cho, "Đang chờ thử lại")
                continue
            raise


# ============== CÁC HÀM XỬ LÝ VIDEO ==============

def upload_va_cho_xu_ly(video_path):
    """Upload video lên Gemini và chờ xử lý xong, trả về video_file. Dùng chung cho cả phân tích và hỏi đáp."""
    print(f"Đang tải {video_path} lên Gemini...")
    video_file = client.files.upload(file=video_path)  # tham số đúng là 'file', không phải 'path'

    while video_file.state.name == "PROCESSING":
        spin_sleep(5, "Đang xử lý video, vui lòng đợi")
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        raise Exception("Google không thể xử lý video này.")

    return video_file


def phan_tich_video(video_path):
    """Phân tích 1 video, trả về nội dung nhật ký thí nghiệm (text)."""
    video_file = upload_va_cho_xu_ly(video_path)

    prompt = """
    Bạn là trợ lý nghiên cứu khoa học. Hãy xem video thí nghiệm này và viết nhật ký thí nghiệm gồm:
    1. Mục tiêu thí nghiệm.
    2. Các bước thực hiện chính.
    3. Các thông số, hóa chất và nồng độ được nhắc đến.
    4. Các lưu ý hoặc quan sát bất thường.
    5. Đề xuất bước tiếp theo.
    """

    response = call_gemini_with_retry([prompt, video_file])

    # Xóa file đã upload sau khi dùng xong, tránh tốn quota lưu trữ trên server Google
    try:
        client.files.delete(name=video_file.name)
    except Exception:
        pass

    return response.text


def ten_log_cua(video_filename):
    """Tính tên file log .md tương ứng với 1 video, không phụ thuộc đuôi viết hoa/thường (.mp4/.MP4)."""
    return os.path.splitext(video_filename)[0] + "_log.md"


def luu_log(video_path, noi_dung):
    """Lưu nhật ký phân tích ra file .md cùng thư mục với script, tên giống tên video."""
    output_name = ten_log_cua(os.path.basename(video_path))
    with open(output_name, "w", encoding="utf-8") as f:
        f.write(noi_dung)
    return output_name


def chon_video(muc_dich):
    """Hiện danh sách video .mp4 trong thư mục, cho người dùng chọn 1, trả về full path (hoặc None nếu hủy)."""
    if not os.path.exists(folder_path):
        print(f"LỖI: Thư mục {folder_path} không tồn tại!")
        return None

    mp4_files = [f for f in os.listdir(folder_path) if f.lower().endswith(".mp4")]
    if not mp4_files:
        print("Không có video nào trong thư mục.")
        return None

    print(f"\nChọn video muốn {muc_dich} (gõ 0 để hủy):")
    for i, f in enumerate(mp4_files, start=1):
        da_co_log = " [đã có log]" if os.path.exists(ten_log_cua(f)) else ""
        print(f"  {i}. {f}{da_co_log}")

    lua_chon = input("Nhập số thứ tự: ").strip()
    if lua_chon == "0" or not lua_chon:
        return None
    if not lua_chon.isdigit() or not (1 <= int(lua_chon) <= len(mp4_files)):
        print("Lựa chọn không hợp lệ.")
        return None

    return os.path.join(folder_path, mp4_files[int(lua_chon) - 1])


# ============== TÙY CHỌN 1: QUÉT & PHÂN TÍCH TẤT CẢ VIDEO MỚI ==============

def quet_va_phan_tich_moi():
    """Quét thư mục, tự động phân tích các video CHƯA có log. Video đã có log sẽ được bỏ qua."""
    print(f"\nBắt đầu quét thư mục: {folder_path}")

    for filename in os.listdir(folder_path):
        if filename.lower().endswith(".mp4"):
            full_path = os.path.join(folder_path, filename)
            output_name = ten_log_cua(filename)

            if os.path.exists(output_name):
                print(f"\n⏭️  Bỏ qua {filename} (đã có log: {output_name})")
                continue

            print(f"\n--- Xử lý file: {filename} ---")
            try:
                noi_dung = phan_tich_video(full_path)
                luu_log(full_path, noi_dung)
                print(f"\n✅ Đã hoàn thành! Kết quả lưu tại: {output_name}")
            except Exception as e:
                print(f"\n❌ Lỗi khi xử lý {filename}: {e}")

    print("\n--- ĐÃ QUÉT XONG TẤT CẢ VIDEO MỚI ---")


# ============== TÙY CHỌN 2: CHỌN 1 VIDEO CỤ THỂ ĐỂ PHÂN TÍCH ==============

def phan_tich_mot_video_tuy_chon():
    """Cho người dùng chọn đúng 1 video để phân tích, kể cả video đã có log (sẽ hỏi trước khi ghi đè)."""
    video_path = chon_video("phân tích")
    if not video_path:
        return

    filename = os.path.basename(video_path)
    output_name = ten_log_cua(filename)

    if os.path.exists(output_name):
        xac_nhan = input(f"⚠️  Video này đã có log ({output_name}), phân tích lại sẽ GHI ĐÈ log cũ. Tiếp tục? (y/n): ").strip().lower()
        if xac_nhan != "y":
            print("Đã hủy.")
            return

    print(f"\n--- Phân tích: {filename} ---")
    try:
        noi_dung = phan_tich_video(video_path)
        luu_log(video_path, noi_dung)
        print(f"\n✅ Đã hoàn thành! Kết quả lưu tại: {output_name}")
    except Exception as e:
        print(f"\n❌ Lỗi khi xử lý {filename}: {e}")


# ============== TÙY CHỌN 3: HỎI ĐÁP VỀ VIDEO ==============

def ask_about_video(video_path):
    """Cho người dùng đặt câu hỏi tự do về hành động/nội dung trong video, Gemini trả lời.
    Trả về True nếu người dùng muốn đổi sang hỏi video khác ngay, False nếu muốn dừng hẳn."""
    try:
        video_file = upload_va_cho_xu_ly(video_path)
    except Exception as e:
        print(f"❌ Lỗi khi tải video: {e}")
        return False

    print("✅ Video đã sẵn sàng. Hãy đặt câu hỏi về hành động/nội dung trong video.")
    print("(Gõ 'doi video' để hỏi video khác, 'thoat' để dừng hỏi đáp)\n")

    # Lưu lại lịch sử hỏi-đáp trong phiên này để Gemini có thể tham khảo câu hỏi trước
    history = ""
    muon_doi_video = False

    while True:
        question = input("Câu hỏi của bạn: ").strip()
        if not question or question.lower() in ("thoat", "exit", "quit"):
            break
        if question.lower() in ("doi video", "đổi video", "doi", "switch"):
            muon_doi_video = True
            break

        full_prompt = (
            "Bạn là trợ lý nghiên cứu khoa học, đang xem video thí nghiệm này. "
            "Hãy trả lời câu hỏi dựa trên những gì quan sát được trong video, "
            "trả lời ngắn gọn, đúng trọng tâm.\n\n"
        )
        if history:
            full_prompt += f"Các câu hỏi/trả lời trước đó trong phiên hỏi đáp này:\n{history}\n"
        full_prompt += f"Câu hỏi hiện tại: {question}"

        try:
            response = call_gemini_with_retry([full_prompt, video_file])
            answer = response.text
            print(f"\n🤖 Gemini: {answer}\n")
            history += f"Hỏi: {question}\nTrả lời: {answer}\n\n"
        except Exception as e:
            print(f"\n❌ Lỗi khi hỏi Gemini: {e}\n")

    try:
        client.files.delete(name=video_file.name)
    except Exception:
        pass

    return muon_doi_video


def hoi_dap_video():
    """Cho chọn video để hỏi đáp. Nếu người dùng gõ 'doi video' lúc đang hỏi, quay lại chọn video
    khác ngay (không cần thoát ra menu chính rồi vào lại)."""
    while True:
        video_path = chon_video("hỏi đáp")
        if not video_path:
            return
        muon_doi = ask_about_video(video_path)
        if not muon_doi:
            return


# ============== MENU CHÍNH ==============

def menu_chinh():
    while True:
        print("\n================ MENU LAB TOOL ================")
        print("1. Quét & phân tích các video MỚI (bỏ qua video đã có log)")
        print("2. Chọn 1 video cụ thể để phân tích (có thể ghi đè log cũ)")
        print("3. Hỏi đáp về một video")
        print("4. Thoát")
        print("=================================================")
        lua_chon = input("Chọn (1-4): ").strip()

        if lua_chon == "1":
            quet_va_phan_tich_moi()
        elif lua_chon == "2":
            phan_tich_mot_video_tuy_chon()
        elif lua_chon == "3":
            hoi_dap_video()
        elif lua_chon == "4":
            print("\n--- KẾT THÚC ---")
            break
        else:
            print("Lựa chọn không hợp lệ, vui lòng chọn lại.")


if __name__ == "__main__":
    menu_chinh()
