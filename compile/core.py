import posixpath
import collections

from .  import codegen
from .. import const, parse


class CodeGenerator (codegen.MutableCode):

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self._inner_assignment = None

    def child_name(self, default):
        '''Create a name for a code object that is inside this one.

            :param default: the name to fall back to if none is specified
               in the source code.

        '''
        name = str(self._inner_assignment or default)
        return ('{}' if name.isidentifier() else '<{}>').format(name)

    def load(self, *es):
        '''Push the result of evaluating some expressions onto the stack.

            :param es: expressions to evaluate.

            NOTE this method always evaluates the expression again, even if
              it was already `load`ed. Use `DUP_TOP` when necessary.

        '''

        for e in es:

            hasattr(e, 'location') and self.mark(e)

            if isinstance(e, parse.tree.Expression):

                self.call(*e)

            elif isinstance(e, parse.tree.Link):

                self.loadop(
                    'LOAD_DEREF' if e in self.cellvars else
                    'LOAD_FAST'  if e in self.varnames else
                    'LOAD_DEREF' if e in self.enclosed else
                    'LOAD_NAME'  if self.slowlocals    else
                    'LOAD_GLOBAL', arg=e, delta=1
                )

            elif isinstance(e, parse.tree.Constant):

                self.loadop('LOAD_CONST', arg=e.value, delta=1)

            else:

                self.loadop('LOAD_CONST', arg=e, delta=1)

    def loadop(self, opcode, *args, delta, arg=...):
        '''Add an opcode, feeding it some items from the value stack.

            :param opcode: see :meth:`codegen.MutableCode.append`.
            :param arg:    see :meth:`codegen.MutableCode.append`. Default: `len(args)`.
            :param args:   items to push onto the stack first.
            :param delta:  value stack size difference.

            NOTE it's impossible use `Ellipsis` as an argument.
              However, since the only opcode that can make use of it
              is `LOAD_CONST`, the use of `LOAD_NAME "Ellipsis"` is advised.

        '''

        self.load(*args)
        return self.append(opcode, len(args) if arg is ... else arg, delta - len(args))

    def store_top(self, var):
        '''Take an item off the value stack, put it in a variable.

            :param var: one of the supported left-hand statements of `=`.

        '''

        type, target, misc = parse.syntax.assignment_target(var)

        if type == const.AT.UNPACK:

            # `target` -- a list of items
            # `misc`   -- number of items to the right and left of a starred one
            ln, star = misc
            #     w/o a starred item                 w/ a starred item
            op  = 'UNPACK_SEQUENCE' if star < 0 else 'UNPACK_EX'
            arg = ln                if star < 0 else star + 256 * (ln - star - 1)
            self.loadop(op, arg=arg, delta=ln - 1)

            for item in target:

                self.store_top(item)

        elif type == const.AT.ATTR:

            # `target` -- the attribute
            # `misc`   -- its owner
            self.loadop('STORE_ATTR', misc, arg=target, delta=-1)

        elif type == const.AT.ITEM:

            # `target` -- the key
            # `misc`   -- the container
            self.loadop('STORE_SUBSCR', misc, target, delta=-1)

        else:

            # `target` -- name of the variable
            # `misc`   -- unused
            self.loadop(
                # XXX isn't it bad design if a `=` in a closure
                #     modifies enclosed variables by default?
                'STORE_DEREF' if target in self.enclosed else
                'STORE_DEREF' if target in self.cellvars else
                'STORE_NAME'  if self.slowlocals else
                'STORE_FAST', arg=target, delta=-1
            )

    def make_function(self, code, defaults, kwdefaults):
        '''Create a function given an immutable code object.

            :param code:       a `CodeType`.
            :param defaults:   as in `FunctionType.__defaults__`.
            :param kwdefaults: as in `FunctionType.__kwdefaults__`.

            FIXME since there's no way to modify `co_freevars` anymore,
              any local variables created after this function will be ignored.
              Yes, this breaks recursion unless you create a variable by
              assigning something to it first.

        '''

        for k, v in kwdefaults.items():

            self.load(str(k), v)

        self.load(*defaults)

        # I used to separate MAKE_CLOSURE from MAKE_FUNCTION,
        # but it turns out there's no real difference. See `Python/ceval.c`.
        for freevar in code.co_freevars:

            self.loadop('LOAD_CLOSURE', arg=self.cellify(freevar), delta=1)

        self.loadop('BUILD_TUPLE', arg=len(code.co_freevars), delta=1 - len(code.co_freevars))
        self.loadop(
            # Python 3.3+ only now.
            'MAKE_CLOSURE', code, self.qualname + '.' + code.co_name,
            arg  =    len(defaults) + 256 * len(kwdefaults),
            delta=1 - len(defaults) -   2 * len(kwdefaults) - bool(code.co_freevars)
        )

    def nativecall(self, f, args, preloaded, infix):
        '''Call a function, ignore all macros.

            :param args: a list of unparsed (i.e. `StructMixIn`) arguments.
            :param preloaded: how many arguments are already on the stack.
            :param infix: whether to disable keyword arguments and varargs.

        '''

        (a, _, _, kw, va, vkw) = (args, (), (), {}, (), ()) if infix \
                            else parse.syntax.argspec(args, definition=False)

        self.load(f, *args)

        for k, v in kw.items():

            self.load(str(k), v)

        self.loadop(
            'CALL_FUNCTION' + '_VAR' * bool(va) + '_KW' * bool(vkw), *va + vkw,
            arg  = len(a) + 256 * len(kw) + preloaded,
            # Minus callable, plus result.
            delta=-len(a) -   2 * len(kw) - preloaded
        )

    def loadcall(self, args):
        '''If there's a single object, load it. Otherwise, call a function.'''

        self.load(*args) if len(args) < 2 else self.call(*args)

    def infixbindl(self, f, arg):
        '''Default implementation of a left infix bind.'''

        self.loadop('CALL_FUNCTION', parse.tree.Link('bind'), f, arg, arg=2, delta=-1)

    def infixbindr(self, f, args):
        '''Default implementation of a right infix bind.'''

        self.load(parse.tree.Link('bind'))
        self.loadop('CALL_FUNCTION', parse.tree.Link('flip'), f, arg=1, delta=1)
        self.loadcall(args)
        self.loadop('CALL_FUNCTION', arg=2, delta=-1)

    def call(self, f, *args, rightbind=False):
        '''Call a function or delegate to a macro.

            function argument ... keyword: value *: varargs **: varkwargs

        '''

        return self.call(*args, rightbind=True) if f.infix and f == '' \
          else INFIXR[f](self, f,  args) if f.infix and not f.closed and rightbind      \
          else INFIXL[f](self, f, *args) if f.infix and not f.closed and len(args) == 1 \
          else PREFIX[f](self, f,  args) if isinstance(f, parse.tree.Link) and f in PREFIX \
          else self.nativecall(f, args, 0, f.infix and not f.closed)

    def store(self, target, expr):
        '''Store the result of `expr` in `target`.

            target = expr

        '''

        self._inner_assignment, e = repr(target), self._inner_assignment
        self.loadop('DUP_TOP', expr, delta=2)
        self._inner_assignment = e
        self.store_top(target)

    def function(self, args, body):
        '''Create a function on the value stack.

            args -> body

        '''

        a, kw, da, dkw, va, vkw = parse.syntax.argspec(args, definition=True)
        n = []
        t = {}

        for index, arg in enumerate(a):

            if isinstance(arg, parse.tree.Link) and arg not in n:

                n.append(arg)

            else:

                n.append('pattern-' + str(index))
                targets['pattern-' + str(index)] = arg

        code = CodeGenerator(self.child_name('<lambda>'), self.qualname,True, n, kw, va, vkw, self)

        getattr(self, 'cellhook', lambda _: None)(code)

        for name, pattern in targets.items():

            code.loadop('LOAD_FAST', arg=name, delta=1)
            code.store_top(pattern)

        code.loadop('RETURN_VALUE', body, delta=0)
        self.make_function(code.compiled, da, dkw)

    def chain(self, a, *bs):
        '''Evaluate expressions one by one, discarding all results but last.

            a
            b1
            b2
            ...

        '''

        self.load(a)

        for b in bs:

            self.loadop('POP_TOP', delta=-1)
            self.load(b)

PREFIX = {}
INFIXL = collections.defaultdict(lambda: CodeGenerator.infixbindl)
INFIXR = collections.defaultdict(lambda: CodeGenerator.infixbindr)
