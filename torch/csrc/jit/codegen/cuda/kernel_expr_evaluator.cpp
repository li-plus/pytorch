
#include <torch/csrc/jit/codegen/cuda/instrumentation.h>
#include <torch/csrc/jit/codegen/cuda/kernel_expr_evaluator.h>

#include <iostream>

namespace torch {
namespace jit {
namespace fuser {
namespace cuda {
namespace kir {

namespace {

template <typename T>
c10::optional<IntOrDouble> toOptionalIntOrDouble(c10::optional<T> i) {
  if (!i) {
    return c10::nullopt;
  }
  return IntOrDouble(i.value());
}

} // namespace

void ExpressionEvaluator::bind(const Val* value, IntOrDouble concrete_value) {
  TORCH_CHECK(value->isScalar());
  TORCH_CHECK(
      value->dtype() == DataType::Int || value->dtype() == DataType::Double);
  TORCH_CHECK(!value->isConstScalar(), "Tried to bind to a constant value");
  TORCH_CHECK(
      value->definition() == nullptr,
      "Tried to bind to a value that is computed in the kernel IR: ",
      value->toInlineString(),
      " with ",
      concrete_value);
  known_values_[value] = concrete_value;
}

void ExpressionEvaluator::bind(
    ParallelType pt,
    Int::ScalarType concrete_value) {
  TORCH_INTERNAL_ASSERT(isParallelTypeThread(pt));
  if (precomputed_values_) {
    // Need to bind the thread value to integer machine
    //  in pre-computed mode.
    precomputed_values_->bindConcreteParallelTypeValue(pt, concrete_value);
  } else {
    auto pdim_string = stringifyThreadSize(pt);
    known_named_scalars_[pdim_string] = concrete_value;
  }
}

c10::optional<IntOrDouble> ExpressionEvaluator::evaluate(const Val* value) {
  if (precomputed_values_ && precomputed_values_->ready()) {
    if (precomputed_values_->getMaybeValueFor(value).has_value()) {
      return toOptionalIntOrDouble(
          precomputed_values_->getMaybeValueFor(value));
    }
  }

  auto maybe_concrete_value = getValue(value);
  if (!maybe_concrete_value.has_value()) {
    if (value->definition() != nullptr) {
      FUSER_PERF_SCOPE("kir::ExpressionEvaluator::evaluate");
      OptInConstDispatch::handle(value->definition());
      maybe_concrete_value = getValue(value);
    }
  }
  return maybe_concrete_value;
}

c10::optional<IntOrDouble> ExpressionEvaluator::getValue(const Val* value) {
  TORCH_INTERNAL_ASSERT(
      value->isAnInt() || value->isADouble(),
      value->toString(),
      " is not a supported type in expression evaluation.");

  if (value->isScalar() && value->isConst()) {
    if (value->isADouble()) {
      return toOptionalIntOrDouble(value->as<Double>()->value());
    }
    return toOptionalIntOrDouble(value->as<Int>()->value());
  }

  if (value->isA<NamedScalar>()) {
    const auto it = known_named_scalars_.find(value->as<NamedScalar>()->name());
    if (it != known_named_scalars_.end()) {
      return c10::optional<IntOrDouble>(it->second);
    }
  }

  const auto it = known_values_.find(value);
  return it != known_values_.end() ? c10::optional<IntOrDouble>(it->second)
                                   : c10::nullopt;
}

void ExpressionEvaluator::print() const {
  std::cout << "\nEvaluation context\n";
  std::cout << "--------------------\n";
  for (const auto& kv : known_values_) {
    TORCH_INTERNAL_ASSERT(!kv.first->isConstScalar());
    std::cout << kv.first << " = " << kv.second << " ; "
              << *kv.first->getValType() << "\n";
  }

  for (const auto& kv : known_named_scalars_) {
    std::cout << kv.first << " = " << kv.second << " ;\n";
  }

  std::cout << "\nPre-computed Values\n";
  if (precomputed_values_ != nullptr) {
    precomputed_values_->print();
  }
  std::cout << "--------------------\n\n";
}

void ExpressionEvaluator::handle(const UnaryOp* uop) {
  using namespace IntOrDouble_functions;
  const auto in = evaluate(uop->in());
  if (in.has_value()) {
    switch (uop->getUnaryOpType()) {
      case UnaryOpType::Neg:
        known_values_[uop->out()] = -*in;
        break;
      case UnaryOpType::Set:
        known_values_[uop->out()] = *in;
        break;
      case UnaryOpType::Cast:
        if (uop->out()->getDataType() == DataType::Int) {
          known_values_[uop->out()] = in->cast<int64_t>();
        } else if (uop->out()->getDataType() == DataType::Double) {
          known_values_[uop->out()] = in->cast<double>();
        } else {
          TORCH_INTERNAL_ASSERT(false, "dtype not supported in evaluator");
        }
        break;
      case UnaryOpType::Abs:
        known_values_[uop->out()] = abs(*in);
        break;
      default:
        TORCH_CHECK(
            false,
            "Unexpected operator type ",
            uop->getUnaryOpType(),
            " in ",
            uop->toString());
    }
  }
}

void ExpressionEvaluator::handle(const BinaryOp* bop) {
  using namespace IntOrDouble_functions;
  const auto lhs = evaluate(bop->lhs());
  const auto rhs = evaluate(bop->rhs());
  if (lhs.has_value() && rhs.has_value()) {
    switch (bop->getBinaryOpType()) {
      case BinaryOpType::Add:
        known_values_[bop->out()] = *lhs + *rhs;
        break;
      case BinaryOpType::Sub:
        known_values_[bop->out()] = *lhs - *rhs;
        break;
      case BinaryOpType::Mul:
        known_values_[bop->out()] = *lhs * *rhs;
        break;
      case BinaryOpType::Div:
        TORCH_CHECK(*rhs != 0);
        known_values_[bop->out()] = *lhs / *rhs;
        break;
      case BinaryOpType::Mod:
        TORCH_CHECK(*rhs != 0);
        known_values_[bop->out()] = *lhs % *rhs;
        break;
      case BinaryOpType::CeilDiv:
        TORCH_CHECK(*rhs != 0);
        known_values_[bop->out()] = ceildiv(*lhs, *rhs);
        break;
      case BinaryOpType::And:
        known_values_[bop->out()] = Int::ScalarType(*lhs && *rhs);
        break;
      case BinaryOpType::Max:
        known_values_[bop->out()] = max(*lhs, *rhs);
        break;
      case BinaryOpType::Min:
        known_values_[bop->out()] = min(*lhs, *rhs);
        break;
      default:
        TORCH_CHECK(!"Unexpected operator type");
    }
  }
}

} // namespace kir
} // namespace cuda
} // namespace fuser
} // namespace jit
} // namespace torch
