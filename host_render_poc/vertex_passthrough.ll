source_filename = "vertex_passthrough.metal"
target datalayout = "e-p:64:64:64"
target triple = "air64-apple-macosx14.0.0"

define <4 x float> @vmain(<4 x float> %position) local_unnamed_addr #0 {
  ret <4 x float> %position
}

attributes #0 = { nounwind }

!air.vertex = !{!0}
!0 = !{ptr @vmain, !1, !2}
!1 = !{!3}
!2 = !{!4}
!3 = !{!"air.position", !"air.arg_type_name", !"float4"}
!4 = !{i32 0, !"air.vertex_input", !"air.location_index", i32 0, i32 1, !"air.arg_type_name", !"float4", !"air.arg_name", !"position"}
