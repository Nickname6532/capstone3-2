/**
 * 전화번호 입력 자동 하이픈 포맷터. DB에 항상 같은 형식(010-1234-5678)으로
 * 저장되도록, 사용자가 숫자만 입력해도 자동으로 하이픈을 채운다.
 * 백스페이스로 지울 때도 하이픈이 "끼여서" 안 지워지는 문제 없이,
 * 하이픈 바로 뒤에서 지우면 그 앞 숫자까지 함께 지워지도록 처리한다.
 */

function formatKoreanPhone(value) {
  const d = value.replace(/\D/g, "").slice(0, 11);
  if (d.startsWith("02")) {
    if (d.length < 3) return d;
    if (d.length < 6) return `${d.slice(0, 2)}-${d.slice(2)}`;
    if (d.length < 10) return `${d.slice(0, 2)}-${d.slice(2, 5)}-${d.slice(5)}`;
    return `${d.slice(0, 2)}-${d.slice(2, 6)}-${d.slice(6, 10)}`;
  }
  if (d.length < 4) return d;
  if (d.length < 8) return `${d.slice(0, 3)}-${d.slice(3)}`;
  if (d.length < 11) return `${d.slice(0, 3)}-${d.slice(3, 6)}-${d.slice(6)}`;
  return `${d.slice(0, 3)}-${d.slice(3, 7)}-${d.slice(7, 11)}`;
}

function attachPhoneAutoFormat(input) {
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Backspace") return;
    if (input.selectionStart !== input.selectionEnd) return; // 드래그 선택 삭제는 기본 동작 사용
    const pos = input.selectionStart;
    if (pos > 0 && input.value[pos - 1] === "-") {
      e.preventDefault();
      const raw = input.value.slice(0, pos - 2) + input.value.slice(pos);
      input.value = formatKoreanPhone(raw);
      const newPos = Math.max(pos - 2, 0);
      input.setSelectionRange(newPos, newPos);
    }
  });

  input.addEventListener("input", () => {
    const pos = input.selectionStart;
    const digitsBeforeCaret = input.value.slice(0, pos).replace(/\D/g, "").length;
    input.value = formatKoreanPhone(input.value);

    let count = 0;
    let newPos = input.value.length;
    for (let i = 0; i < input.value.length; i++) {
      if (/\d/.test(input.value[i])) count++;
      if (count === digitsBeforeCaret) {
        newPos = i + 1;
        break;
      }
    }
    input.setSelectionRange(newPos, newPos);
  });
}
