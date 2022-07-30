import abc
from typing import List, Optional, Type, Dict, Set

from django.db.models import (
    Expression,
    Model,
    ExpressionWrapper,
    OuterRef,
    Subquery,
    Value,
)

from baserow.contrib.database.formula.ast.tree import (
    BaserowFunctionCall,
    BaserowFunctionDefinition,
    ArgCountSpecifier,
    BaserowExpression,
)
from baserow.contrib.database.formula.expression_generator.django_expressions import (
    AndExpr,
)
from baserow.contrib.database.formula.types.formula_type import (
    BaserowFormulaType,
    BaserowFormulaValidType,
    UnTyped,
    BaserowSingleArgumentTypeChecker,
    BaserowArgumentTypeChecker,
)


class FixedNumOfArgs(ArgCountSpecifier):
    def __str__(self):
        plural = "" if self.count == 1 else "s"
        return f"exactly {self.count} argument{plural}"

    def test(self, num_args):
        return self.count == num_args


class NumOfArgsGreaterThan(ArgCountSpecifier):
    def __str__(self):
        return f"more than {self.count} arguments"

    def test(self, num_args):
        return self.count < num_args


class ZeroArgumentBaserowFunction(BaserowFunctionDefinition):
    """
    A helper sub type of a BaserowFunctionDefinition that lets the
    user talk specifically about a func with no arguments when implementing. Without
    this normal classes implementing BaserowFunctionDefinition need to faff
    about accessing argument lists etc.
    """

    @property
    def arg_types(self) -> BaserowArgumentTypeChecker:
        return []

    @property
    def num_args(self) -> ArgCountSpecifier:
        return FixedNumOfArgs(0)

    @abc.abstractmethod
    def type_function(
        self,
        func_call: BaserowFunctionCall[UnTyped],
    ) -> BaserowExpression[BaserowFormulaType]:
        """
        Override this function to type and optionally transform an untyped function
        call to this function def.

        You can perform any logic you require here and return entirely different or
        transformed typed expressions. However by default most
        of the time if your function doesn't need to do different things based on
        the types of it's arguments all you need to do is something like:
        ```
        return func_call.with_valid_type(INSERT VALID TYPE OF FUNC HERE)
        ```

        :param func_call: An untyped function call to this function which needs typing.
        :return: A typed BaserowExpression, most probably just the original func_call
            but with a type, but any expression could be returned here.
        """

        pass

    @abc.abstractmethod
    def to_django_expression(self) -> Expression:
        """
        Override this function to return a Django Expression which calculates the result
        of this function.
        Only will be called if all arguments passed the type check
        and a valid type for the function has been returned from type_function.

        :return: A Django Expression which when evaluated calculates the results of this
            function call.
        """

        pass

    def type_function_given_valid_args(
        self,
        args: List[BaserowExpression[BaserowFormulaValidType]],
        func_call: BaserowFunctionCall[UnTyped],
    ) -> BaserowExpression[BaserowFormulaType]:
        return self.type_function(func_call)

    def to_django_expression_given_args(
        self,
        args: List[Expression],
        model: Type[Model],
        model_instance: Optional[Model],
        pre_annotations: Dict[str, Expression],
        aggregate_filters: List[Expression],
        join_ids: Set[str],
    ) -> Expression:
        return self.to_django_expression()

    def call_and_type_with(self) -> BaserowFunctionCall[BaserowFormulaType]:
        return self.call_and_type_with_args([])


class OneArgumentBaserowFunction(BaserowFunctionDefinition):
    """
    A helper sub type of a BaserowFunctionDefinition that lets the
    user talk specifically about the single argument in a one arg func when implementing
    . Without this normal classes implementing BaserowFunctionDefinition need to faff
    about accessing argument lists etc.
    """

    aggregate = False

    @property
    def arg_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the single argument
        provided to this function. Only when the argument meets the type requirement
        will type_function be called with the argument that matches.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def arg_types(self) -> BaserowArgumentTypeChecker:
        return [self.arg_type]

    @property
    def num_args(self) -> ArgCountSpecifier:
        return FixedNumOfArgs(1)

    @abc.abstractmethod
    def type_function(
        self,
        func_call: BaserowFunctionCall[UnTyped],
        arg: BaserowExpression[BaserowFormulaValidType],
    ) -> BaserowExpression[BaserowFormulaType]:
        """
        Override this function to type and optionally transform an untyped function
        call to this function def. The single argument has already been type checked
        according to the self.arg_type property, this method will only be called if it
        matches.

        You can perform any logic you require here and return entirely different or
        transformed typed expressions based on the arguments. However by default most
        of the time if your function doesn't need to do different things based on
        the types of it's arguments all you need to do is something like:
        ```
        return func_call.with_valid_type(INSERT VALID TYPE OF FUNC HERE)
        ```

        :param func_call: An untyped function call to this function which needs typing.
        :param arg: The valid typed single argument from func_call provided already
            extracted from func_call for you to inspect.
        :return: A typed BaserowExpression, most probably just the original func_call
            but with a type, but any expression could be returned here.
        """

        pass

    @abc.abstractmethod
    def to_django_expression(self, arg: Expression) -> Expression:
        """
        Override this function to return a Django Expression which calculates the result
        of this function given that the single arg has already been converted to a
        Django Expression. Only will be called if all arguments passed the type check
        and a valid type for the function has been returned from type_function.

        :param arg: The already converted arg expression to use.
        :return: A Django Expression which when evaluated calculates the results of this
            function call.
        """

        pass

    def type_function_given_valid_args(
        self,
        args: List[BaserowExpression[BaserowFormulaValidType]],
        func_call: BaserowFunctionCall[UnTyped],
    ) -> BaserowExpression[BaserowFormulaType]:
        arg = args[0]
        if self.aggregate and not arg.many:
            func_call.with_invalid_type(
                "first argument must be an aggregate formula"
            )

        expr = self.type_function(func_call, arg)
        if self.aggregate:
            expr.many = False
        return expr

    def to_django_expression_given_args(
        self,
        args: List[Expression],
        model: Type[Model],
        model_instance: Optional[Model],
        pre_annotations: Dict[str, Expression],
        aggregate_filters: List[Expression],
        join_ids: Set[str],
    ) -> Expression:
        django_expr = self.to_django_expression(args[0])
        if self.aggregate:
            return aggregate_wrapper(
                django_expr, model, pre_annotations, aggregate_filters, join_ids
            )
        else:
            return django_expr

    def call_and_type_with(
        self, arg: BaserowExpression[BaserowFormulaType]
    ) -> BaserowFunctionCall[BaserowFormulaType]:
        return self.call_and_type_with_args([arg])


def aggregate_wrapper(
    aggregate_func_expr, model, pre_annotations, aggregate_filters, join_ids
):
    if len(aggregate_filters) > 0:
        combined_filter = Value(True)
        for f in aggregate_filters:
            combined_filter = AndExpr(combined_filter, f)
        aggregate_func_expr.filter = combined_filter
        aggregate_filters.clear()

    # We need to enforce that each filtered relation is not null so django generates us
    # inner joins.
    not_null_filters_for_inner_join = {
        f"{key}__isnull": False for key in pre_annotations
    }

    expr = ExpressionWrapper(
        Subquery(
            model.objects_and_trash.annotate(**pre_annotations)
            .filter(id=OuterRef("id"), **not_null_filters_for_inner_join)
            .values(result=aggregate_func_expr),
        ),
        output_field=aggregate_func_expr.output_field,
    )
    pre_annotations.clear()
    join_ids.clear()
    return expr


class TwoArgumentBaserowFunction(BaserowFunctionDefinition):
    """
    A helper sub type of a BaserowFunctionDefinition that lets the
    user talk specifically about the two arguments in a two arg func when implementing.
    Without this normal classes implementing BaserowFunctionDefinition need to faff
    about accessing argument lists etc.
    """

    aggregate = False

    @property
    def arg1_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the first arg
        provided to this function. Only when all arguments meet the type requirements
        defined in the argX_type properties will type_function be called.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def arg2_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the second arg
        provided to this function. Only when all arguments meet the type requirements
        defined in the argX_type properties will type_function be called.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def arg_types(self) -> BaserowArgumentTypeChecker:
        return [self.arg1_type, self.arg2_type]

    @property
    def num_args(self) -> ArgCountSpecifier:
        return FixedNumOfArgs(2)

    @abc.abstractmethod
    def type_function(
        self,
        func_call: BaserowFunctionCall[UnTyped],
        arg1: BaserowExpression[BaserowFormulaValidType],
        arg2: BaserowExpression[BaserowFormulaValidType],
    ) -> BaserowExpression[BaserowFormulaType]:
        """
        Override this function to type and optionally transform an untyped function
        call to this function def. The arguments have already been type checked
        according to the self.arg1_type and self.arg2_type properties this method will
        only be called if they match.

        You can perform any logic you require here and return entirely different or
        transformed typed expressions based on the arguments. However by default most
        of the time if your function doesn't need to do different things based on
        the types of it's arguments all you need to do is something like:
        ```
        return func_call.with_valid_type(INSERT VALID TYPE OF FUNC HERE)
        ```

        :param func_call: An untyped function call to this function which needs typing.
        :param arg1: The valid typed first argument from func_call provided already
            extracted from func_call for you to inspect.
        :param arg2: The valid typed second argument from func_call provided already
            extracted from func_call for you to inspect.
        :return: A typed BaserowExpression, most probably just the original func_call
            but with a type, but any expression could be returned here.
        """

        pass

    @abc.abstractmethod
    def to_django_expression(self, arg1: Expression, arg2: Expression) -> Expression:
        """
        Override this function to return a Django Expression which calculates the result
        of this function given that the args have already been converted to
        Django Expressions. Only will be called if all arguments passed the type check
        and a valid type for the function has been returned from type_function.

        :param arg1: The already converted first arg expression to use.
        :param arg2: The already converted second arg expression to use.
        :return: A Django Expression which when evaluated calculates the results of this
            function call.
        """

        pass

    def type_function_given_valid_args(
        self,
        args: List[BaserowExpression[BaserowFormulaValidType]],
        func_call: BaserowFunctionCall[UnTyped],
    ) -> BaserowExpression[BaserowFormulaType]:
        if self.aggregate and not args[0].many and not args[1].many:
            func_call.with_invalid_type(
                "one of the two arguments must be an aggregate formula"
            )

        expr = self.type_function(func_call, args[0], args[1])
        if self.aggregate:
            expr.many = False
        return expr

    def to_django_expression_given_args(
        self,
        args: List[Expression],
        model: Type[Model],
        model_instance: Optional[Model],
        pre_annotations: Dict[str, Expression],
        aggregate_filters: List[Expression],
        join_ids: Set[str],
    ) -> Expression:
        django_expr = self.to_django_expression(args[0], args[1])
        if self.aggregate:
            return aggregate_wrapper(
                django_expr, model, pre_annotations, aggregate_filters, join_ids
            )
        else:
            return django_expr

    def call_and_type_with(
        self,
        arg1: BaserowExpression[BaserowFormulaType],
        arg2: BaserowExpression[BaserowFormulaType],
    ) -> BaserowFunctionCall[BaserowFormulaType]:
        return self.call_and_type_with_args([arg1, arg2])


class ThreeArgumentBaserowFunction(BaserowFunctionDefinition):
    @property
    def arg_types(self) -> BaserowArgumentTypeChecker:
        return [self.arg1_type, self.arg2_type, self.arg3_type]

    @property
    def arg1_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the first arg
        provided to this function. Only when all arguments meet the type requirements
        defined in the argX_type properties will type_function be called.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def arg2_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the second arg
        provided to this function. Only when all arguments meet the type requirements
        defined in the argX_type properties will type_function be called.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def arg3_type(self) -> BaserowSingleArgumentTypeChecker:
        """
        Override this property to set the required argument type for the third arg
        provided to this function. Only when all arguments meet the type requirements
        defined in the argX_type properties will type_function be called.

        The default definition if not overridden will allow all valid formula types.

        :return: A BaserowSingleArgumentTypeChecker
        """

        return [BaserowFormulaValidType]

    @property
    def num_args(self) -> ArgCountSpecifier:
        return FixedNumOfArgs(3)

    @abc.abstractmethod
    def type_function(
        self,
        func_call: BaserowFunctionCall[UnTyped],
        arg1: BaserowExpression[BaserowFormulaValidType],
        arg2: BaserowExpression[BaserowFormulaValidType],
        arg3: BaserowExpression[BaserowFormulaValidType],
    ) -> BaserowExpression[BaserowFormulaType]:
        """
        Override this function to type and optionally transform an untyped function
        call to this function def. The arguments have already been type checked
        according to the self.arg1_type, self.arg2_type and self.arg3_type properties
        this method will only be called if they all match.

        You can perform any logic you require here and return entirely different or
        transformed typed expressions based on the arguments. However by default most
        of the time if your function doesn't need to do different things based on
        the types of it's arguments all you need to do is something like:
        ```
        return func_call.with_valid_type(INSERT VALID TYPE OF FUNC HERE)
        ```

        :param func_call: An untyped function call to this function which needs typing.
        :param arg1: The valid typed first argument from func_call provided already
            extracted from func_call for you to inspect.
        :param arg2: The valid typed second argument from func_call provided already
            extracted from func_call for you to inspect.
        :param arg3: The valid typed third argument from func_call provided already
            extracted from func_call for you to inspect.
        :return: A typed BaserowExpression, most probably just the original func_call
            but with a type, but any expression could be returned here.
        """

        pass

    @abc.abstractmethod
    def to_django_expression(
        self, arg1: Expression, arg2: Expression, arg3: Expression
    ) -> Expression:
        """
        Override this function to return a Django Expression which calculates the result
        of this function given that the args have already been converted to
        Django Expressions. Only will be called if all arguments passed the type check
        and a valid type for the function has been returned from type_function.

        :param arg1: The already converted first arg expression to use.
        :param arg2: The already converted second arg expression to use.
        :param arg3: The already converted third arg expression to use.
        :return: A Django Expression which when evaluated calculates the results of this
            function call.
        """

        pass

    def type_function_given_valid_args(
        self,
        args: List[BaserowExpression[BaserowFormulaValidType]],
        func_call: BaserowFunctionCall[UnTyped],
    ) -> BaserowExpression[BaserowFormulaType]:
        return self.type_function(func_call, args[0], args[1], args[2])

    def to_django_expression_given_args(
        self,
        args: List[Expression],
        model: Type[Model],
        model_instance: Optional[Model],
        pre_annotations: Dict[str, Expression],
        aggregate_filters: List[Expression],
        join_ids: Set[str],
    ) -> Expression:
        return self.to_django_expression(args[0], args[1], args[2])

    def call_and_type_with(
        self,
        arg1: BaserowExpression[BaserowFormulaType],
        arg2: BaserowExpression[BaserowFormulaType],
        arg3: BaserowExpression[BaserowFormulaType],
    ) -> BaserowFunctionCall[BaserowFormulaType]:
        return self.call_and_type_with_args([arg1, arg2, arg3])
