# Gradient Barriers

The gradient barriers are loaded from the `gradient_barriers_table` defined in the model parameters (the default is `support.gradient_barries`).

Gradient barriers are only included in the analysis if the `structure_types` parameter in the model parameters include `gradients`.  Otherwise gradient are excluded from the analysis.

A copy of the gradient barriers used in the analysis is made in the `<output_scheam>.gradient_barriers` file.

See `gradient_barriers` for a tool to generate barriers.


