import sys

import ops
import blorb
from txt import warn, err

def to_signed_word(word):
    if word & 0x8000:
        return -((~word & 0xffff)+1)
    return word
def to_signed_char(char):
    if char & 0x80:
        return -((~char & 0xff)+1)
    return char

def b16_setter(base):
    def setter(self, val):
        val &= 0xffff
        self.env.mem[base] = val >> 8
        self.env.mem[base+1] = val & 0xff
    return setter

def u16_prop(base):
    def getter(self): return self.env.u16(base)
    return property(fget=getter, fset=b16_setter(base))

def s16_prop(base):
    def getter(self): return self.env.s16(base)
    return property(fget=getter, fset=b16_setter(base))

def u8_prop(base):
    def getter(self): return self.env.u8(base)
    def setter(self, val): self.env.mem[base] = val & 0xff
    return property(fget=getter, fset=setter)

class Header(object):
        
    def __init__(self, env):
        self.env = env

    version = u8_prop(0x0)
    flags1 = u8_prop(0x1)
    release = u16_prop(0x2)
    high_mem_base = u16_prop(0x4)
    pc = u16_prop(0x6)
    dict_base = u16_prop(0x8)
    obj_tab_base = u16_prop(0xA)
    global_var_base = u16_prop(0xC)
    static_mem_base = u16_prop(0xE)
    flags2 = u16_prop(0x10)

    def serial_getter(self): return self.env.mem[0x12:0x18]
    def serial_setter(self, val): self.env.mem[0x12:0x18] = val
    serial = property(fget=serial_getter, fset=serial_setter)

    abbrev_base = u16_prop(0x18)
    file_len = u16_prop(0x1A)
    checksum = u16_prop(0x1C)

    #everything after 1C is after v3

    interp_number = u8_prop(0x1E)
    interp_version = u8_prop(0x1F)

    screen_height_lines = u8_prop(0x20)
    screen_width_chars = u8_prop(0x21)
    screen_width_units = u16_prop(0x22)
    screen_height_units = u16_prop(0x24)

    font_width_units = u8_prop(0x26)
    font_height_units = u8_prop(0x27)

    routine_offset = u16_prop(0x28) #div'd by 8
    string_offset = u16_prop(0x2A) #div'd by 8

    default_bg_color = u8_prop(0x2C)
    default_fg_color = u8_prop(0x2D)

    term_chars_base = u16_prop(0x2E)
    std_rev_number = u16_prop(0x32)
    alpha_tab_base = u16_prop(0x34)
    hdr_ext_tab_base = u16_prop(0x36)

class VarForm:
    pass
class ShortForm:
    pass
class ExtForm:
    pass
class LongForm:
    pass

class ZeroOperand:
    pass
class OneOperand:
    pass
class TwoOperand:
    pass
class VarOperand:
    pass

def get_opcode_form(env, opcode):
    if opcode == 190:
        return ExtForm
    sel = (opcode & 0xc0) >> 6
    if sel == 0b11:
        return VarForm
    elif sel == 0b10:
        return ShortForm
    else:
        return LongForm

def get_operand_count(opcode, form):
    if form == VarForm:
        if (opcode >> 5) & 1:
            return VarOperand
        return TwoOperand
    if form == ShortForm:
        if (opcode >> 4) & 3 == 3:
            return ZeroOperand
        return OneOperand
    if form == ExtForm:
        return VarOperand
    return TwoOperand # LongForm

class WordSize:
    pass
class ByteSize:
    pass
class VarSize:
    pass

def get_operand_sizes(szbyte):
    sizes = []
    offset = 6
    while offset >= 0 and (szbyte >> offset) & 3 != 3:
        size = (szbyte >> offset) & 3
        if size == 0b00:
            sizes.append(WordSize)
        elif size == 0b01:
            sizes.append(ByteSize)
        else: # 0b10
            sizes.append(VarSize)
        offset -= 2
    return sizes

def set_standard_flags(env):
    if env.hdr.version < 4:
        # no variable-spaced font (bit 6 = 0)
        # no screen splitting available (bit 5 = 0)
        # no status line (bit 4 = 0)
        env.hdr.flags1 &= 0b10001111
    else:
        # no timed keyboard events available (bit 7 = 0)
        # no sound effects available (bit 5 = 0)
        # no italic available (bit 3 = 0)
        # no boldface available (bit 2 = 0)
        # no picture display available (bit 1 = 0)
        # no color available (bit 0 = 0)
        env.hdr.flags1 &= 0b01010000
        # fixed-space font available (bit 4 = 1)
        env.hdr.flags1 |= 0b00010000
        # use the apple 2e interp # to fix Beyond Zork compat
        env.hdr.interp_number = 2

        env.hdr.screen_width_chars = 80
        env.hdr.screen_height_lines = 40

        env.hdr.screen_width_units = 80
        env.hdr.screen_height_units = 40

        env.hdr.font_width_units = 1
        env.hdr.font_height_units = 1


def check_and_set_dyn_flags(env):
    # supposed to check what game wants here and react when
    # things change, but instead let's just always clear the
    # features we don't support which are:
    # menus (bit 8 (flags2 is 16 bits, so bits go 0-15)
    # sound effects (bit 7)
    # mouse (bit 5)
    # undo (bit 4)
    # and pictures (bit 3)
    env.hdr.flags2 &= 0b1111111001000111

class Env:
    def __init__(self, mem):
        self.orig_mem = mem
        self.mem = map(ord, mem)
        self.hdr = Header(self)
        self.pc = self.hdr.pc
        self.callstack = [ops.Frame(0)]

        # to make quetzal saves easier
        self.last_pc_branch_var = None
        self.last_pc_store_var = None

        self.output_buffer = {
            1: {0:{}, 1:{}},  # screen: top/bottom windows
            2: '', # transcript
            3: '', # mem
            4: ''  # player input (not impld atm)
        }
        self.cursor = {
            # win0 cursor never used (and set_cursor does nothing
            # on win0), but keeping track anyway to simplify
            # code...
            0:(0,0),
            1:(0,0)
        }

        self.selected_ostreams = set([1])
        self.memory_ostream_stack = []
        self.use_buffered_output = True

        self.current_window = 0
        self.top_window_height = 0

        set_standard_flags(self)
    def u16(self, i):
        high = self.u8(i)
        return (high << 8) | self.u8(i+1)
    def s16(self, i):
        w = self.u16(i)
        return to_signed_word(w)
    def u8(self, i):
        return self.mem[i]
    def s8(self, i):
        c = self.u8(i)
        return to_signed_char(c)
    def check_dyn_mem(self, i):
        if i >= self.hdr.static_mem_base:
            err('game tried to write in static mem: '+str(i))
        if i <= 0x36 and i != 0x10:
            err('game tried to write in non-dyn header bytes: '+str(i))
    def write16(self, i, val):
        self.check_dyn_mem(i)
        self.mem[i] = (val >> 8) & 0xff
        self.mem[i+1] = val & 0xff
    def write8(self, i, val):
        self.check_dyn_mem(i)
        self.mem[i] = val & 0xff
    def reset(self):
        # only the bottom two bits of flags2 survive reset
        # (transcribe to printer & fixed pitch font)
        bits_to_save = self.hdr.flags2 & 3
        self.__init__(self.orig_mem)
        self.hdr.flags2 &= ~3
        self.hdr.flags2 |= bits_to_save

class OpInfo:
    def __init__(self, operands, store_var=None, branch_offset=None, branch_on=None, text=None):
        self.operands = operands
        self.store_var = store_var
        self.branch_offset = branch_offset
        self.branch_on = branch_on
        self.text = text

def step(env):

    check_and_set_dyn_flags(env)

    opcode = env.u8(env.pc)
    form = get_opcode_form(env, opcode)
    count = get_operand_count(opcode, form)

    if form == ExtForm:
        opcode = env.u8(env.pc+1)

    if form == ShortForm:
        szbyte = (opcode >> 4) & 3
        szbyte = (szbyte << 6) | 0x3f
        operand_ptr = env.pc+1
        sizes = get_operand_sizes(szbyte)
    elif form == VarForm:
        szbyte = env.u8(env.pc+1)
        operand_ptr = env.pc+2
        sizes = get_operand_sizes(szbyte)
        # handle call_vn2/vs2's extra szbyte
        if opcode in [236, 250]:
            szbyte2 = env.u8(env.pc+2)
            sizes += get_operand_sizes(szbyte2)
            operand_ptr = env.pc+3
    elif form == ExtForm:
        szbyte = env.u8(env.pc+2)
        operand_ptr = env.pc+3
        sizes = get_operand_sizes(szbyte)
    elif form == LongForm:
        operand_ptr = env.pc+1
        sizes = []
        for offset in [6,5]:
            if (opcode >> offset) & 1:
                sizes.append(VarSize)
            else:
                sizes.append(ByteSize)
    else:
        err('unknown opform specified: ' + str(form))

    operands = []
    foundVarStr = ''
    for i in range(len(sizes)):
        if sizes[i] == WordSize:
            operands.append(env.u16(operand_ptr))
            operand_ptr += 2
        elif sizes[i] == ByteSize:
            operands.append(env.u8(operand_ptr))
            operand_ptr += 1
        elif sizes[i] == VarSize:
            var_loc = env.u8(operand_ptr)
            var_value = ops.get_var(env, var_loc)
            operands.append(var_value)
            if DBG:
                varname = ops.get_var_name(var_loc)
                foundVarStr += '      found '+str(var_value)+' in '+varname + '\n'
            operand_ptr += 1
        else:
            err('unknown operand size specified: ' + str(sizes[i]))

    if form == ExtForm:
        dispatch = ops.ext_dispatch
        has_store_var = ops.ext_has_store_var
        has_branch_var = ops.ext_has_branch_var
    else:
        dispatch = ops.dispatch
        has_store_var = ops.has_store_var
        has_branch_var = ops.has_branch_var

    opinfo = OpInfo(operands)

    if has_store_var[opcode]:
        opinfo.store_var = env.u8(operand_ptr)
        env.last_pc_store_var = operand_ptr # to make quetzal saves easier
        operand_ptr += 1

    if has_branch_var[opcode]: # std:4.7
        branch_info = env.u8(operand_ptr)
        env.last_pc_branch_var = operand_ptr # to make quetzal saves easier
        operand_ptr += 1
        opinfo.branch_on = (branch_info & 128) == 128
        if branch_info & 64:
            opinfo.branch_offset = branch_info & 0x3f
        else:
            branch_offset = branch_info & 0x3f
            branch_offset <<= 8
            branch_offset |= env.u8(operand_ptr)
            operand_ptr += 1
            # sign extend 14b # to 16b
            if branch_offset & 0x2000:
                branch_offset |= 0xc000
            opinfo.branch_offset = to_signed_word(branch_offset)

    # handle print_ and print_ret's string operand
    if form != ExtForm and opcode in [178, 179]:
        while True:
            word = env.u16(operand_ptr)
            operand_ptr += 2
            operands.append(word)
            if word & 0x8000:
                break

    last_pc = env.pc

    # After all that, operand_ptr should now point to next op
    env.pc = operand_ptr

    def hex_out(bytes):
        s = ''
        for b in bytes:
            s += hex(b) + ' '
        return s

    if DBG:
        op_hex = hex_out(env.mem[last_pc:env.pc])

        warn('step: pc', hex(last_pc))
        warn('      opcode', opcode)
        warn('      form', form)
        warn('      count', count)
        if opinfo.store_var:
            warn('      store_var', ops.get_var_name(opinfo.store_var))
        if foundVarStr:
            warn(foundVarStr, end='')
        warn('      sizes', sizes)
        warn('      operands', opinfo.operands)
        warn('      next_pc', hex(env.pc))
        #warn('      bytes', op_hex)

    dispatch[opcode](env, opinfo)

DBG = 0

def main():
    if len(sys.argv) != 2:
        print('usage: python zmach.py STORY_FILE.z3')
        sys.exit()

    with open(sys.argv[1], 'rb') as f:
        mem = f.read()
        if blorb.is_blorb(mem):
            mem = blorb.get_code(mem)
        env = Env(mem)

    if env.hdr.version not in range(1,8+1):
        err('unknown z-machine version '+str(env.hdr.version))

    ops.setup_opcodes(env)

    i=0
    while True:
        i += 1
        if DBG:
            warn(i)

        step(env)

if __name__ == '__main__':
    main()

