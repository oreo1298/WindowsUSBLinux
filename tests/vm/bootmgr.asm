; Test stand-in for Windows' bootmgr: GRUB's `ntldr` command loads it at
; 2000:0000 in real mode.  It prints a marker on COM1 and halts.
bits 16
org 0
start:
    cli
    mov ax, cs
    mov ds, ax
    mov dx, 0x3F9           ; IER = 0
    xor al, al
    out dx, al
    mov dx, 0x3FB           ; LCR: DLAB
    mov al, 0x80
    out dx, al
    mov dx, 0x3F8           ; divisor 1 = 115200 baud
    mov al, 1
    out dx, al
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB           ; 8N1
    mov al, 0x03
    out dx, al
    mov si, msg
.next:
    lodsb
    test al, al
    jz .halt
    mov ah, al
.wait:
    mov dx, 0x3FD
    in al, dx
    test al, 0x20
    jz .wait
    mov al, ah
    mov dx, 0x3F8
    out dx, al
    jmp .next
.halt:
    hlt
    jmp .halt
msg db 13, 10, "RUFUX-BIOS-OK", 13, 10, 0
times 8192 - ($ - $$) db 0
